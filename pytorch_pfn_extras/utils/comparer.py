# mypy: ignore-errors

import collections
import re
import threading
import concurrent.futures

import torch.testing

from pytorch_pfn_extras import handler as _handler_module
from pytorch_pfn_extras.training import _trainer
from pytorch_pfn_extras.training import manager as manager_module
from pytorch_pfn_extras.training import _evaluator
from pytorch_pfn_extras.training import trigger as trigger_module


class _ComparableHandler(_handler_module.BaseHandler):
    def __init__(self, handler, name, save_outs_cb):
        self._handler = handler
        self._save_outs_cb = save_outs_cb
        self.name = name
        self.iteration = 0

    def convert_batch(self, args):
        return self._handler.convert_batch(args)

    def train_setup(self, trainer, loader):
        return self._handler.train_setup(trainer, loader)

    def train_epoch_begin(self, trainer, loader):
        return self._handler.train_epoch_begin(trainer, loader)

    def train_epoch_end(self, trainer):
        return self._handler.train_epoch_end(trainer)

    def train_validation_begin(self, evaluator):
        return self._handler.train_validation_begin(evaluator)

    def train_validation_end(self, trainer, evaluator):
        return self._handler.train_validation_end(trainer, evaluator)

    def train_step(self, trainer, batch_idx, batch, complete_fn):
        return self._handler.train_step(trainer, batch_idx, batch, complete_fn)

    def train_post_step(self, trainer, batch_idx, batch, outputs):
        self._handler.train_post_step(trainer, batch_idx, batch, outputs)
        self.iteration += 1
        return self._save_outs_cb(self, trainer.models, batch_idx, outputs)

    def eval_loop_begin(self, evaluator):
        return self._handler.eval_loop_begin(evaluator)

    def eval_step(self, evaluator, batch_idx, batch, complete_fn):
        return self._handler.eval_step(
            evaluator, batch_idx, batch, complete_fn)

    def eval_loop_end(self, evaluator):
        return self._handler.eval_loop_end(evaluator)

    def eval_post_step(self, evaluator, batch_idx, batch, outputs):
        self._handler.eval_post_step(evaluator, batch_idx, batch, outputs)
        self.iteration += 1
        return self._save_outs_cb(self, evaluator.models, batch_idx, outputs)


def get_default_comparer(rtol=1e-07, atol=0, equal_nan=True, msg=None):
    """Creates default comparer function.

    The created function will compare the outputs by using
    `torch.testing.assert_allclose` with specified options.

    Args:
        rtol (float): Relative tolerance.
        atol (float): Absolute tolerance.
        equal_nan (bool): If ``True``, NaNs will be ignored.
        msg (str): Error message to be printed in case of failure.
    """
    def compare_fn(backend1, backend2, name, val1, val2):
        err_msg = msg or f" comparing {backend1} and {backend2} in {name}"
        torch.testing.assert_allclose(
            # TODO select the device where
            # the tensors will be compared?
            val1.cpu().detach(),
            val2.cpu().detach(),
            rtol=rtol,
            atol=atol,
            equal_nan=equal_nan,
            msg=err_msg,
        )
    return compare_fn


_default_comparer = get_default_comparer()


class Comparer:

    def __init__(
            self,
            *,
            trigger=None,
            compare_fn=_default_comparer,
            concurrency=None,
            outputs=True,
            params=False,
    ):
        self._engine_type = None
        self._engines = collections.OrderedDict()
        self._compare_fn = compare_fn
        self._targets = {}
        self._output_keys = (outputs,) if isinstance(outputs, str) else outputs
        self._param_keys = (params,) if isinstance(params, str) else params
        self._preprocessed_keys = None
        self._finalized = False
        self._concurrency = concurrency
        self._barrier = None
        self._report_lock = threading.Lock()
        self._semaphore = None

        if trigger is None:
            self._trigger = trigger_module.get_trigger((1, "epoch"))
        else:
            self._engine_type = _trainer.Trainer
            self._trigger = trigger_module.get_trigger(trigger)

    def _preprocess_keys(self, model):
        if self._param_keys is False:
            return []
        sdict = model.state_dict()
        if self._param_keys is True:
            return list(sdict.keys())
        preprocessed_keys = []
        for tc_k in self._param_keys:
            matched = False
            for sd_k in sdict.keys():
                if re.match(tc_k, sd_k) is not None:
                    preprocessed_keys.append(sd_k)
                    matched = True
            if not matched:
                raise ValueError(
                    f'didnt find a match for {tc_k} in the model')
        return preprocessed_keys

    def _add_target(self, handle, models, outputs):
        targets = {}

        # Preprocess
        if self._output_keys is True:
            self._output_keys = list(outputs.keys())
        elif self._output_keys is False:
            self._output_keys = []

        if self._preprocessed_keys is None:
            self._preprocessed_keys = self._preprocess_keys(models['main'])

        targets.update({key: outputs[key] for key in self._output_keys})
        if len(self._preprocessed_keys) > 0:
            sdict = models['main'].state_dict()
            targets.update({key: sdict[key] for key in self._preprocessed_keys})
        self._targets[handle.name] = targets

    def _assert_incompatible_trigger(self, condition):
        if not condition:
            raise ValueError("Engines have different triggers.")

    def _compare_outs(self):
        names = list(self._engines.keys())
        backend1 = names[0]
        for backend2 in names[1:]:
            for val_name in self._targets[backend1].keys():
                out1 = self._targets[backend1][val_name]
                out2 = self._targets[backend2][val_name]
                self._compare_fn(backend1, backend2, val_name, out1, out2)

    def _compare_targets(self, handle, models, batch_idx, outputs):
        engine, _, _ = self._engines[handle.name]
        if hasattr(engine, "manager"):
            class _ManagerProxy(manager_module._ManagerProxy):
                @property
                def iteration(self) -> int:
                    return self._manager.iteration + 1

            manager = _ManagerProxy(engine.manager)
            if not self._trigger(manager):
                return

        # Save the outputs of this iteration
        with self._report_lock:
            self._add_target(handle, models, outputs)
            if len(self._targets.keys()) == len(self._engines.keys()):
                # all outputs have been filled, lets compare and reset
                self._compare_outs()
                self._targets = {}
            self._assert_incompatible_trigger(not self._finalized)

        # Excplicitly synchronize
        self._semaphore.release()
        self._barrier.wait()
        self._semaphore.acquire()

    def add_engine(self, name, engine, *args, **kwargs):
        type_engine = type(engine)

        if type_engine not in (_trainer.Trainer, _evaluator.Evaluator):
            raise ValueError(f"Engine type {type_engine} is not supported")

        if self._engine_type is None:
            self._engine_type = type_engine
        elif type_engine != self._engine_type:
            raise ValueError("All the engines must be of the same type")

        if name in self._engines.keys():
            raise ValueError(f"Engine {engine} already registered")

        self._engines[name] = engine, args, kwargs
        engine.handler = _ComparableHandler(engine.handler, name, self._compare_targets)

    def run_engine(self, engine, args, kwargs):
        try:
            self._semaphore.acquire()
            engine.run(*args, **kwargs)
            with self._report_lock:
                self._finalized = True
                self._assert_incompatible_trigger(len(self._targets) == 0)
        except Exception:
            self._barrier.abort()
            raise
        finally:
            self._semaphore.release()

    def dump(self, engine, dir, train_loader, val_loader):
        raise NotImplementedError

    def compare(self):
        """Compares outputs.

        Args:
            loaders (dict of loaders):
                Data loaders used as input for each engine.
        """
        n_workers = len(self._engines)
        self._barrier = threading.Barrier(n_workers)
        self._semaphore = threading.Semaphore(
            n_workers if self._concurrency is None else self._concurrency)
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = []
            for _, (engine, args, kwargs) in self._engines.items():
                futures.append(executor.submit(self.run_engine, engine, args, kwargs))
            for future in concurrent.futures.as_completed(futures):
                future.result()

    def compare_with_dump(self, dir):
        raise NotImplementedError
