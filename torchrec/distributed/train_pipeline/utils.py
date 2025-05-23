#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict
import abc

import contextlib
import copy
import itertools
import logging
from collections import defaultdict, deque, OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass

from itertools import chain
from threading import Event, Thread
from typing import (
    Any,
    Callable,
    cast,
    Deque,
    Dict,
    Generator,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import torch
from torch import distributed as dist
from torch.utils.hooks import RemovableHandle

if not torch._running_with_deploy():
    from torch.distributed._composable.fsdp.fully_shard import FSDPModule as FSDP2
else:

    class FSDP2:
        pass


from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.fx.immutable_collections import (
    immutable_dict as fx_immutable_dict,
    immutable_list as fx_immutable_list,
)
from torch.fx.node import Node
from torch.nn.modules.module import _IncompatibleKeys
from torch.profiler import record_function
from torchrec.distributed.dist_data import KJTAllToAll, KJTAllToAllTensorsAwaitable
from torchrec.distributed.embedding_sharding import (
    FusedKJTListSplitsAwaitable,
    KJTListSplitsAwaitable,
    KJTSplitsAllToAllMeta,
)
from torchrec.distributed.embedding_types import KJTList
from torchrec.distributed.model_parallel import DistributedModelParallel, ShardedModule
from torchrec.distributed.train_pipeline.pipeline_context import (
    EmbeddingTrainPipelineContext,
    In,
    Out,  # noqa
    PrefetchTrainPipelineContext,
    TrainPipelineContext,
)

from torchrec.distributed.types import Awaitable, LazyNoWait

from torchrec.sparse.jagged_tensor import JaggedTensor, KeyedJaggedTensor, KeyedTensor
from torchrec.streamable import Multistreamable, Pipelineable

logger: logging.Logger = logging.getLogger(__name__)

StageOut = TypeVar("StageOut", bound=Pipelineable)
RunnableType = Callable[..., StageOut]
StageOutputWithEvent = Tuple[Optional[StageOut], Optional[torch.Event]]


@dataclass
class PipelineStage:
    """
    A pipeline stage represents a transform to an input that is independent of the
    backwards() of the model. Examples include batch H2D transfer, GPU postproc, or
    gradient-less model processing.

    Args:
        name (str): Name of the stage.
        runnable (Callable[In, Out]): Function that performs a gradient-less
            transform.
        stream (torch.cuda.streams.Stream): Stream to run on. Often each stage has a
            unique stream, but having different pipelines share a stream provides more
            synchronization semantics.
        fill_callback (Optional[Callable[[], None]])) - optional step to run after the main
            runnable during filling the pipeline
        data_exhausted_callback (Optional[Callable[[], None]])) - optional callback to run
            when data is ehxausted
    """

    name: str
    runnable: RunnableType
    stream: torch.Stream
    fill_callback: Optional[Callable[[], None]] = None
    data_exhausted_callback: Optional[Callable[[], None]] = None


class BaseArgInfoStep(abc.ABC):
    @abc.abstractmethod
    # pyre-ignore
    def process(self, arg) -> Any:
        raise Exception("Not implemented in the BaseArgInfoStep")

    def __eq__(self, other: object) -> bool:
        """
        Some tests use the equality checks on the ArgInfo and/or CallArgs, so it's
        natural to use dataclasses for ArgInfoStep implementations. However
        Torchrec doesn't like dataclasses: https://github.com/pytorch/pytorch/issues/74909

        So, this class creates a makeshift generic implementation similar to dataclass, but without
        dataclass.
        """
        if not isinstance(other, type(self)):
            return False
        return all(
            getattr(self, field_name) == getattr(other, field_name)
            for field_name in self.__dict__.keys()
        )


class NoopArgInfoStep(BaseArgInfoStep):
    # pyre-ignore
    def process(self, arg) -> Any:
        return arg


class GetAttrArgInfoStep(BaseArgInfoStep):
    def __init__(self, attr_name: str) -> None:
        super().__init__()
        self.attr_name = attr_name

    # pyre-ignore
    def process(self, arg) -> Any:
        return getattr(arg, self.attr_name)


class GetItemArgInfoStep(BaseArgInfoStep):
    def __init__(self, item_index: Union[str, int]) -> None:
        super().__init__()
        self.item_index = item_index

    # pyre-ignore
    def process(self, arg) -> Any:
        return arg[self.item_index]


class PostprocArgInfoStep(BaseArgInfoStep):
    def __init__(self, postproc_module: "PipelinedPostproc") -> None:
        super().__init__()
        self.postproc_module = postproc_module

    # pyre-ignore
    def process(self, arg) -> Any:
        return self.postproc_module(arg)


class ScalarArgInfoStep(BaseArgInfoStep):
    def __init__(self, value: object) -> None:
        super().__init__()
        self.value = value

    # pyre-ignore
    def process(self, _arg) -> Any:
        return self.value


class ListArgInfoStep(BaseArgInfoStep):
    def __init__(self, value: List[object]) -> None:
        super().__init__()
        self.value = value

    # pyre-ignore
    def process(self, arg) -> Any:
        return [
            (v if not isinstance(v, ArgInfo) else v.process_steps(arg))
            for v in self.value
        ]


class DictArgInfoStep(BaseArgInfoStep):
    def __init__(self, value: Dict[str, object]) -> None:
        super().__init__()
        self.value = value

    # pyre-ignore
    def process(self, arg) -> Any:
        return {
            k: (v if not isinstance(v, ArgInfo) else v.process_steps(arg))
            for k, v in self.value.items()
        }


class ArgInfoStepFactory:
    """
    Convenience class to reduce the amount of imports the external uses will have.
    Should closely follow the constructor interfaces for the corresponding classes.
    """

    @classmethod
    def noop(cls) -> NoopArgInfoStep:
        return NoopArgInfoStep()

    @classmethod
    def get_attr(cls, name: str) -> GetAttrArgInfoStep:
        return GetAttrArgInfoStep(name)

    @classmethod
    def get_item(cls, index: Union[str, int]) -> GetItemArgInfoStep:
        return GetItemArgInfoStep(index)

    @classmethod
    def postproc(
        cls, pipelined_postproc_module: "PipelinedPostproc"
    ) -> PostprocArgInfoStep:
        return PostprocArgInfoStep(pipelined_postproc_module)

    @classmethod
    def from_scalar(cls, value: object) -> ScalarArgInfoStep:
        return ScalarArgInfoStep(value)

    @classmethod
    def from_list(cls, value: List[object]) -> ListArgInfoStep:
        return ListArgInfoStep(value)

    @classmethod
    def from_dict(cls, value: Dict[str, object]) -> DictArgInfoStep:
        return DictArgInfoStep(value)


@dataclass
class ArgInfo:
    """
    Representation of args from a node.

    Attributes:
        steps (List[ArgInfoStep]): sequence of transformations from input batch.
            Steps can be thought of consequtive transformations on the input, with
            output of previous step used as an input for the next. I.e. for 3 steps
            it is similar to step3(step2(step1(input)))
            See `BaseArgInfoStep` class hierearchy for supported transformations
    """

    steps: List[BaseArgInfoStep]

    def add_step(self, step: BaseArgInfoStep) -> "ArgInfo":
        self.steps.insert(0, step)
        return self

    def append_step(self, step: BaseArgInfoStep) -> "ArgInfo":
        self.steps.append(step)
        return self

    # pyre-ignore[3]
    def process_steps(
        self,
        arg: Any,  # pyre-ignore[2]
    ) -> Any:
        if not self.steps:
            return None
        for step in self.steps:
            arg = step.process(arg)

        return arg


@dataclass
class CallArgs:
    args: List[ArgInfo]
    kwargs: Dict[str, ArgInfo]

    # pyre-ignore[3]
    def build_args_kwargs(
        self, initial_input: Any  # pyre-ignore[2]
    ) -> Tuple[List[Any], Dict[str, Any]]:
        args = [arg.process_steps(initial_input) for arg in self.args]
        kwargs = {
            key: arg.process_steps(initial_input) for key, arg in self.kwargs.items()
        }
        return args, kwargs


def recursive_record_stream(
    # pyre-fixme[2]: Parameter `re` must have a type that does not contain `Any`
    res: Union[torch.Tensor, Pipelineable, Iterable[Any], Dict[Any, Any]],
    stream: torch.Stream,
) -> None:
    if isinstance(res, torch.Tensor) and res.device.type in ["cuda", "mtia"]:
        res.record_stream(stream)
    elif isinstance(res, Pipelineable):
        res.record_stream(stream)
    elif isinstance(res, (list, tuple)):
        for v in res:
            recursive_record_stream(v, stream)
    elif isinstance(res, dict):
        for v in res.values():
            recursive_record_stream(v, stream)


class NoOpStream:
    """No-Op Context manager that takes in a stream"""

    def __init__(self, stream: Optional[torch.Stream]) -> None:
        self._stream = stream

    def __enter__(self) -> "NoOpStream":
        """Return `self` upon entering the runtime context."""
        return self

    # pyre-ignore
    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class PipelinedPostproc(torch.nn.Module):
    """
    Wrapper around postproc module found during model graph traversal for sparse data dist
    pipelining. In addition to the original module, it encapsulates information needed for
    execution such as list of ArgInfo and the current training pipeline context.

    Args:
        postproc_module (torch.nn.Module): postproc module to run
        fqn (str): fqn of the postproc module in the model being pipelined
        args (CallArgs): CallArgs for the postproc module
        context (TrainPipelineContext): Training context for the next iteration / batch

    Returns:
        Any

    Example:
        postproc = PipelinedPostproc(postproc_module, fqn, args, context)
        # module-swap with pipeliend postproc
        setattr(model, fqn, postproc)
    """

    _FORCE_STATE_DICT_LOAD = True

    def __init__(
        self,
        postproc_module: torch.nn.Module,
        fqn: str,
        args: CallArgs,
        context: TrainPipelineContext,
        # TODO: make streams non-optional - skipping now to avoid ripple effect
        default_stream: Optional[torch.Stream],
        dist_stream: Optional[torch.Stream],
    ) -> None:
        super().__init__()
        self._postproc_module = postproc_module
        self._fqn = fqn
        self._args = args
        self._context = context
        self._default_stream = default_stream
        self._dist_stream = dist_stream
        if not default_stream:
            logger.warning(
                f"Postproc module {fqn} has no default stream. This may cause race conditions and NaNs during training!"
            )
        if not dist_stream:
            logger.warning(
                f"Postproc module {fqn} has no dist stream. This may cause race conditions and NaNs during training!"
            )

        if self._dist_stream:
            device: torch.device = self._dist_stream.device
            # pyre-ignore
            self._stream_context = (
                torch.get_device_module(device).stream
                if device.type in ["cuda", "mtia"]
                else torch.cuda.stream
            )
        else:
            self._stream_context = NoOpStream

    @property
    def postproc_module(self) -> torch.nn.Module:
        return self._postproc_module

    @property
    def fqn(self) -> str:
        return self._fqn

    # pyre-ignore
    def forward(self, *input, **kwargs) -> Any:
        """
        Args:
            Any args and kwargs during model fwd
            During _start_data_dist, input[0] contains the current data
        Returns:
            Any
        """
        if self._fqn in self._context.postproc_fwd_results:
            # This should only be hit in two cases:
            # 1) During model forward
            # During model forward, avoid duplicate work
            # by returning the cached result from previous
            # iteration's _start_data_dist
            # 2) During _start_data_dist when postproc module is
            # shared by more than one args. e.g. if we have
            # postproc_out_a = postproc_a(input)
            # postproc_out_b = postproc_b(postproc_out_a) <- postproc_a shared
            # postproc_out_c = postproc_c(postproc_out_a) <-^
            # When processing postproc_b, we cache value of postproc_a(input)
            # so when processing postproc_c, we can reuse postproc_a(input)
            res = self._context.postproc_fwd_results[self._fqn]
            return res

        # Everything below should only be called during _start_data_dist stage

        # Build up arg and kwargs from recursive call to pass to postproc module
        # Arguments to postproc module can be also be a derived product
        # of another postproc module call, as long as module is pipelineable

        # Use input[0] as _start_data_dist only passes 1 arg
        args, kwargs = self._args.build_args_kwargs(input[0])

        with record_function(f"## sdd_input_postproc {self._context.index} ##"):
            # should be no-op as we call this in dist stream
            with self._stream_context(self._dist_stream):
                res = self._postproc_module(*args, **kwargs)

            # Ensure postproc modules output is safe to use from default stream later
            if self._default_stream and self._dist_stream:
                self._default_stream.wait_stream(self._dist_stream)

                if isinstance(res, (torch.Tensor, Pipelineable, Iterable, Dict)):
                    # Result from module forward might be a complex type such as
                    # Tuple[KeyedJaggedTensor, Dict[str, torch.Tensor]]
                    # In this case, we need to first iterate over each element of tuple
                    # and call record_stream on first item as KJT is Pipelineable
                    # for the second item (Dict), we iterate over the values and call
                    # record_stream accordingly.

                    # pyre-ignore[6]
                    recursive_record_stream(res, self._default_stream)
                elif self._context.index == 0:
                    logger.warning(
                        f"Result of postproc module {self._fqn} is of type {type(res)}. We currently expect it to be a Tensor, Pipelineable, Iterable, or Dict to handle memory safety. If your output is not of this type, please add support for it above. Otherwise you might run into NaNs or CUDA Illegal Memory issues during training!"
                    )

            with self._stream_context(self._default_stream):
                # Cache results, only during _start_data_dist
                self._context.postproc_fwd_results[self._fqn] = res

            return res

    @property
    def args(self) -> CallArgs:
        return self._args

    def set_context(self, context: TrainPipelineContext) -> None:
        self._context = context

    def get_context(self) -> TrainPipelineContext:
        return self._context

    def named_modules(
        self,
        memo: Optional[Set[torch.nn.Module]] = None,
        prefix: str = "",
        remove_duplicate: bool = True,
    ) -> Iterator[Tuple[str, torch.nn.Module]]:
        if memo is None:
            memo = set()
        if self not in memo:
            if remove_duplicate:
                memo.add(self)
            # This is needed because otherwise the rewrite won't find the existing postproc, and will create a new one
            # Also, `named_modules` need to include self - see base implementation in the nn.modules.Module
            yield prefix, self
            # Difference from base implementation is here - the child name (_postproc_module) is not added to the prefix
            yield from self._postproc_module.named_modules(
                memo, prefix, remove_duplicate
            )

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.nn.Parameter]]:
        yield from self._postproc_module.named_parameters(
            prefix,
            recurse,
            remove_duplicate,
        )

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        yield from self._postproc_module.named_buffers(
            prefix, recurse, remove_duplicate
        )

    # pyre-ignore [14]
    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        # super().state_dict(destination, prefix, keep_vars)
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()
        self._postproc_module.state_dict(
            destination=destination, prefix=prefix, keep_vars=keep_vars
        )
        return destination

    # pyre-ignore [14]
    def load_state_dict(
        self,
        state_dict: OrderedDict[str, torch.Tensor],
        strict: bool = True,
    ) -> _IncompatibleKeys:
        return self._postproc_module.load_state_dict(state_dict, strict=strict)


TForwardContext = TypeVar("TForwardContext", bound=TrainPipelineContext)

EmbeddingModuleRetType = Union[Dict[str, JaggedTensor], KeyedTensor]


class BaseForward(Generic[TForwardContext]):
    def __init__(
        self,
        name: str,
        args: CallArgs,
        module: ShardedModule,
        context: TForwardContext,
        stream: Optional[torch.Stream] = None,
    ) -> None:
        self._name = name
        self._args = args
        self._module = module
        self._context = context
        self._stream = stream
        self._device: torch.device = stream.device if stream else torch.device("cuda")

    @property
    def name(self) -> str:
        return self._name

    @property
    def args(self) -> CallArgs:
        return self._args

    def set_context(self, context: TForwardContext) -> None:
        self._context = context

    def get_context(self) -> TForwardContext:
        return self._context


class PipelinedForward(BaseForward[TrainPipelineContext]):
    """
    This pipeline is used in TrainPipelineSparseDist
    """

    # pyre-ignore [2, 24]
    def __call__(self, *input, **kwargs) -> Awaitable:
        assert (
            self._name in self._context.input_dist_tensors_requests
        ), "Invalid PipelinedForward usage, please do not directly call model.forward()"
        request = self._context.input_dist_tensors_requests.pop(self._name)
        assert isinstance(request, Awaitable)
        with record_function("## wait_sparse_data_dist ##"):
            # Finish waiting on the dist_stream,
            # in case some delayed stream scheduling happens during the wait() call.
            with torch.get_device_module(self._device).stream(self._stream):
                data = request.wait()

        # Make sure that both result of input_dist and context
        # are properly transferred to the current stream.
        ctx = self._context.module_contexts.pop(self._name)

        if self._stream is not None:
            torch.get_device_module(self._device).current_stream().wait_stream(
                self._stream
            )
            cur_stream = torch.get_device_module(self._device).current_stream()

            assert isinstance(
                data, (torch.Tensor, Multistreamable)
            ), f"{type(data)} must implement Multistreamable interface"
            data.record_stream(cur_stream)
            ctx.record_stream(cur_stream)

        return self._module.compute_and_output_dist(ctx, data)


class EmbeddingPipelinedForward(BaseForward[EmbeddingTrainPipelineContext]):
    """
    This pipeline is used in TrainPipelineSemiSync
    """

    def __call__(
        self,
        # pyre-ignore
        *input,
        # pyre-ignore
        **kwargs,
    ) -> Union[
        Awaitable[EmbeddingModuleRetType],
        Tuple[
            Awaitable[EmbeddingModuleRetType], Awaitable[Optional[KeyedJaggedTensor]]
        ],
    ]:
        assert (
            self._name in self._context.embedding_a2a_requests
        ), "Invalid EmbeddingPipelinedForward usage, please do not directly call model.forward()"

        ctx = self._context.module_contexts.pop(self._name)
        cur_stream = torch.get_device_module(self._device).current_stream()

        if self._stream is not None:
            torch.get_device_module(self._device).current_stream().wait_stream(
                self._stream
            )
            ctx.record_stream(cur_stream)

        awaitable = self._context.embedding_a2a_requests.pop(self._name)
        # in case of MC modules
        is_mc_module: bool = isinstance(awaitable, Iterable)
        remapped_kjts: Optional[KeyedJaggedTensor] = None

        if is_mc_module:
            embeddings = awaitable[0].wait()
            remapped_kjts = awaitable[1].wait()
        else:
            assert isinstance(awaitable, Awaitable)
            embeddings = (
                awaitable.wait()
            )  # trigger awaitable manually for type checking

        self.detach_embeddings(embeddings=embeddings, cur_stream=cur_stream)

        if is_mc_module:
            return (LazyNoWait(embeddings), LazyNoWait(remapped_kjts))
        else:
            return LazyNoWait(embeddings)

    def detach_embeddings(
        self,
        embeddings: Union[Dict[str, JaggedTensor], KeyedTensor],
        cur_stream: torch.Stream,
    ) -> None:
        """
        detach the grad from embeddings so that the backward/opt of the embeddings
        won't be invoked by loss.backward(). Instead, there is a dedicated embedding_backward
        call in semi-sync pipeline progress.
        """
        tensors = []
        detached_tensors = []
        # in case of EC, embeddings are Dict[str, JaggedTensor]
        if isinstance(embeddings, Dict):
            for jt in embeddings.values():
                assert isinstance(jt, JaggedTensor)
                tensor = jt.values()
                detached_tensor = tensor.detach().requires_grad_()
                detached_tensor.retain_grad()
                jt._values = detached_tensor
                tensors.append(tensor)
                detached_tensors.append(detached_tensor)
            self._context.embedding_tensors.append(tensors)
            self._context.embedding_features.append(list(embeddings.keys()))
            self._context.detached_embedding_tensors.append(detached_tensors)
        else:
            # in case of EBC, embeddings are KeyedTensor
            assert isinstance(embeddings, KeyedTensor)
            embeddings.record_stream(cur_stream)
            tensor = embeddings.values()
            detached_tensor = tensor.detach().requires_grad_()
            detached_tensor.retain_grad()
            embeddings._values = detached_tensor
            tensors.append(tensor)
            detached_tensors.append(detached_tensor)
            self._context.embedding_tensors.append(tensors)
            """
            KeyedTensor is returned by EmbeddingBagCollections and its variants
            KeyedTensor holds dense data from multiple features and .values()
            returns a single concatenated dense tensor. To ensure that
            context.embedding_tensors[i] has the same length as
            context.embedding_features[i], we pass in a list with a single item:
            a list containing all the embedding feature names.
            """
            self._context.embedding_features.append([list(embeddings.keys())])
            self._context.detached_embedding_tensors.append(detached_tensors)


class InSyncEmbeddingPipelinedForward(EmbeddingPipelinedForward):
    """
    This pipeline is used in TrainPipelineFusedSparseDist
    """

    def detach_embeddings(
        self,
        embeddings: Union[Dict[str, JaggedTensor], KeyedTensor],
        cur_stream: torch.Stream,
    ) -> None:
        # doing nothing
        pass


class PrefetchPipelinedForward(BaseForward[PrefetchTrainPipelineContext]):
    """
    This pipeline is used in PrefetchTrainPipelineSparseDist
    """

    def __init__(
        self,
        name: str,
        args: CallArgs,
        module: ShardedModule,
        context: PrefetchTrainPipelineContext,
        prefetch_stream: Optional[torch.Stream] = None,
    ) -> None:
        super().__init__(
            name=name,
            args=args,
            module=module,
            context=context,
            stream=prefetch_stream,
        )

    # pyre-ignore [2, 24]
    def __call__(self, *input, **kwargs) -> Awaitable:
        assert (
            self._name in self._context.module_input_post_prefetch
        ), "Invalid PrefetchPipelinedForward usage, please do not directly call model.forward()"
        data = self._context.module_input_post_prefetch.pop(self._name)
        ctx = self._context.module_contexts_post_prefetch.pop(self._name)

        # Make sure that both result of input_dist and context
        # are properly transferred to the current stream.
        if self._stream is not None:
            torch.get_device_module(self._device).current_stream().wait_stream(
                self._stream
            )
            cur_stream = torch.get_device_module(self._device).current_stream()

            assert isinstance(
                data, (torch.Tensor, Multistreamable)
            ), f"{type(data)} must implement Multistreamable interface"
            data.record_stream(cur_stream)

            ctx.record_stream(cur_stream)

        return self._module.compute_and_output_dist(ctx, data)


class KJTAllToAllForward:
    def __init__(
        self, pg: dist.ProcessGroup, splits: List[int], stagger: int = 1
    ) -> None:
        self._pg = pg
        self._splits = splits
        self._stagger = stagger
        self._splits_cumsum: List[int] = [0] + list(itertools.accumulate(splits))

    def __call__(self, input: KeyedJaggedTensor) -> KJTSplitsAllToAllMeta:
        with torch.no_grad():
            assert len(input.keys()) == sum(self._splits)
            rank = dist.get_rank(self._pg)
            local_keys = input.keys()[
                self._splits_cumsum[rank] : self._splits_cumsum[rank + 1]
            ]
            input_splits = input.dist_splits(self._splits)
            device = input.values().device
            splits_tensors = [
                torch.tensor(splits, device=device) for splits in input_splits
            ]
            if not input.variable_stride_per_key():
                splits_tensors.append(
                    torch.tensor([input.stride()] * self._pg.size(), device=device)
                )
            return KJTSplitsAllToAllMeta(
                pg=self._pg,
                _input=input,
                splits=self._splits,
                splits_tensors=splits_tensors,
                input_splits=input_splits,
                input_tensors=input.dist_tensors(),
                labels=input.dist_labels(),
                keys=local_keys,
                device=device,
                stagger=self._stagger,
            )


class Tracer(torch.fx.Tracer):
    """
    The Trace class used in `_rewrite_model`, treating all ShardedModules and ShardedModule-free
    modules as leaf modules. A module who is not a ShardedModule but contains ShardedModule would
    NOT be considered as a leaf module.
    """

    # Disables proxying buffers during tracing. Ideally, proxying buffers would be
    # disabled, but some models are currently mutating buffer values, which causes errors
    # during tracing. If those models can be rewritten to not do that, we can likely
    # remove this line.
    proxy_buffer_attributes = False

    def __init__(self, leaf_modules: Optional[List[str]] = None) -> None:
        super().__init__()
        self._leaf_modules: List[str] = leaf_modules if leaf_modules is not None else []

    def is_leaf_module(self, m: torch.nn.Module, module_qualified_name: str) -> bool:
        if (
            isinstance(m, ShardedModule)
            or module_qualified_name in self._leaf_modules
            or isinstance(m, FSDP)
            or isinstance(m, FSDP2)
        ):
            return True
        return super().is_leaf_module(m, module_qualified_name)


def _to_device(batch: In, device: torch.device, non_blocking: bool) -> In:
    assert isinstance(
        batch, (torch.Tensor, Pipelineable)
    ), f"{type(batch)} must implement Pipelineable interface"
    return cast(In, batch.to(device=device, non_blocking=non_blocking))


def _wait_for_batch(batch: In, stream: Optional[torch.Stream]) -> None:
    """
    As mentioned in
    https://pytorch.org/docs/stable/generated/torch.Tensor.record_stream.html, PyTorch
    uses the "caching allocator" for memory allocation for tensors. When a tensor is
    freed, its memory is likely to be reused by newly constructed tenosrs. By default,
    this allocator traces whether a tensor is still in use by only the CUDA stream where
    it was created. When a tensor is used by additional CUDA streams, we need to call
    `record_stream` to tell the allocator about these streams. Otherwise, the allocator
    might free the underlying memory of the tensor once it is no longer used by the
    creator stream. This is a notable programming trick when we write programs using
    multiple CUDA streams.
    """
    if stream is None:
        return

    device = stream.device
    torch.get_device_module(device).current_stream().wait_stream(stream)
    cur_stream = torch.get_device_module(device).current_stream()
    assert isinstance(
        batch, (torch.Tensor, Multistreamable)
    ), f"{type(batch)} must implement Multistreamable interface"
    batch.record_stream(cur_stream)


def _wait_for_events(
    batch: In,
    context: TrainPipelineContext,
    stream: Optional[torch.Stream],
) -> None:
    """
    Wait for any outstanding events for a given context
    """

    for event in context.events:
        event.wait()
    context.events.clear()
    if stream:
        assert isinstance(
            batch, (torch.Tensor, Multistreamable)
        ), f"{type(batch)} must implement Multistreamable interface"
        batch.record_stream(stream)


def _start_data_dist(
    pipelined_modules: List[ShardedModule],
    batch: Pipelineable,
    context: TrainPipelineContext,
) -> None:
    if context.version == 0:
        context.input_dist_splits_requests.clear()
        context.module_contexts_next_batch.clear()
        context.fused_splits_awaitables.clear()

    for module in pipelined_modules:
        forward = module.forward
        assert isinstance(
            forward,
            (
                PipelinedForward,
                PrefetchPipelinedForward,
                EmbeddingPipelinedForward,
                InSyncEmbeddingPipelinedForward,
            ),
        )

        # Retrieve argument for the input_dist of EBC
        # is_getitem True means this argument could be retrieved by a list
        # False means this argument is getting while getattr
        # and this info was done in the _rewrite_model by tracing the
        # entire model to get the arg_info_list
        args, kwargs = forward.args.build_args_kwargs(batch)

        # Start input distribution.
        module_ctx = module.create_context()
        if context.version == 0:
            context.module_contexts_next_batch[forward.name] = module_ctx
        else:
            context.module_contexts[forward.name] = module_ctx
        context.input_dist_splits_requests[forward.name] = module.input_dist(
            module_ctx, *args, **kwargs
        )
    _fuse_input_dist_splits(context)


def _start_embedding_lookup(
    module: ShardedModule,
    context: EmbeddingTrainPipelineContext,
    source_stream: Optional[torch.Stream],
    target_stream: Optional[torch.Stream],
    # pyre-ignore[2]
    stream_context: Callable[..., AbstractContextManager[Any, Any]],
) -> None:
    module_context = context.module_contexts[module.forward.name]
    with stream_context(source_stream):
        kjt = context.input_dist_tensors_requests[module.forward.name].wait()

    if target_stream is not None:
        kjt.record_stream(target_stream)
        module_context.record_stream(target_stream)
    output_dist_out = module.compute_and_output_dist(module_context, kjt)
    context.embedding_a2a_requests[module.forward.name] = output_dist_out


def _fuse_input_dist_splits(context: TrainPipelineContext) -> None:
    names_per_pg = defaultdict(list)
    for name, request in context.input_dist_splits_requests.items():
        pg = None
        if isinstance(request, KJTListSplitsAwaitable):
            for awaitable in request.awaitables:
                if isinstance(awaitable, KJTSplitsAllToAllMeta):
                    pg = awaitable.pg
                    break
        names_per_pg[pg].append(name)

    for pg, names in names_per_pg.items():
        context.fused_splits_awaitables.append(
            (
                names,
                FusedKJTListSplitsAwaitable(
                    # pyre-ignore[6]
                    requests=[
                        context.input_dist_splits_requests[name] for name in names
                    ],
                    contexts=[
                        (
                            context.module_contexts_next_batch[name]
                            if context.version == 0
                            else context.module_contexts[name]
                        )
                        for name in names
                    ],
                    pg=pg,
                ),
            )
        )


def _check_args_for_call_module(
    node: torch.fx.Node,
) -> bool:
    """
    Recursively checks if args to a node is the result of a call_module.
    """
    if node.op == "call_module":
        return True

    for arg in node.args:
        if isinstance(arg, torch.fx.Node) and _check_args_for_call_module(arg):
            return True

    return False


def _check_postproc_pipelineable(
    module: torch.nn.Module,
) -> bool:
    for _, _ in module.named_parameters(recurse=True):
        # Cannot have any trainable params for it to be pipelined
        logger.warning(
            f"Module {module} cannot be pipelined as it has trainable parameters"
        )
        return False
    return True


def _find_postproc_module_recursive(
    module: torch.nn.Module,
    postproc_module_fqn: str,
) -> Optional[torch.nn.Module]:
    """
    Finds the postproc module in the model.
    """
    for name, child in module.named_modules():
        if name == postproc_module_fqn:
            return child
    return None


class NodeArgsHelper:
    def __init__(
        self,
        model: torch.nn.Module,
        context: TrainPipelineContext,
        pipeline_postproc: bool,
        default_stream: Optional[torch.Stream] = None,
        dist_stream: Optional[torch.Stream] = None,
    ) -> None:
        self._model = model
        self._context = context
        self._pipeline_postproc = pipeline_postproc
        self._default_stream = default_stream
        self._dist_stream = dist_stream
        self._pipelined_postprocs: Set[PipelinedPostproc] = set()

    @property
    def pipelined_postprocs(self) -> Set[PipelinedPostproc]:
        return self._pipelined_postprocs

    def _swap_postproc_module_recursive(
        self,
        module: torch.nn.Module,
        to_swap_module: torch.nn.Module,
        postproc_module_fqn: str,
        path: str = "",
    ) -> torch.nn.Module:
        """
        Swaps the postproc module in the model.
        """
        if isinstance(module, PipelinedPostproc):
            return module

        if path == postproc_module_fqn:
            return to_swap_module

        for name, child in module.named_children():
            child = self._swap_postproc_module_recursive(
                child,
                to_swap_module,
                postproc_module_fqn,
                path + "." + name if path else name,
            )
            setattr(module, name, child)

        return module

    def _handle_constant(
        self,
        arg: Any,  # pyre-ignore
        arg_info: ArgInfo,
        for_postproc_module: bool = False,
    ) -> Optional[ArgInfo]:
        if not self._pipeline_postproc:
            return None

        if isinstance(arg, fx_immutable_dict):
            step = ArgInfoStepFactory.from_dict(
                {
                    k: self._handle_collection_element(v, for_postproc_module)
                    for k, v in arg.items()
                }
            )
        elif isinstance(arg, fx_immutable_list):
            step = ArgInfoStepFactory.from_list(
                [self._handle_collection_element(v, for_postproc_module) for v in arg]
            )
        else:
            step = ArgInfoStepFactory.from_scalar(arg)
        arg_info.add_step(step)
        return arg_info

    # pyre-ignore[3]
    def _handle_collection_element(
        self,
        # pyre-ignore[2]
        arg: Any,
        for_postproc_module: bool = False,
    ) -> Any:
        if not isinstance(arg, torch.fx.Node):
            return arg

        arg_info_nested = self._get_node_args_helper_inner(
            arg,
            for_postproc_module,
        )
        return arg_info_nested

    def _handle_placeholder(
        self, child_node: torch.fx.Node, arg_info: ArgInfo
    ) -> ArgInfo:
        # note: mutates arg_info
        if hasattr(child_node, "ph_key"):
            # pyre-fixme[16]
            ph_key: str = child_node.ph_key
            # example: ph_key = 'event_id_list_features_seqs[marketplace]'
            ph_key = ph_key.replace("[", ".")
            ph_keys = ph_key.split(".")
            for key in ph_keys:
                if "]" in key:
                    k_ = key[:-1]
                    try:
                        k_ = int(k_)
                    except ValueError:
                        pass
                    arg_info.append_step(ArgInfoStepFactory.get_item(k_))
                else:
                    arg_info.append_step(ArgInfoStepFactory.get_attr(key))
        else:
            # no-op
            arg_info.add_step(ArgInfoStepFactory.noop())
        return arg_info

    def _handle_module(
        self, child_node: torch.fx.Node, arg_info: ArgInfo
    ) -> Optional[ArgInfo]:
        postproc_module_fqn = str(child_node.target)
        postproc_module = _find_postproc_module_recursive(
            self._model, postproc_module_fqn
        )

        if not self._pipeline_postproc:
            logger.warning(
                f"Found module {postproc_module} that potentially modifies KJ. Train pipeline initialized with `pipeline_postproc=False` (default), so we assume KJT input modification. To allow torchrec to check if this module can be safely pipelined, please set `pipeline_postproc=True`"
            )
            return None

        if not postproc_module:
            # Could not find such module, should not happen
            return None

        if isinstance(postproc_module, PipelinedPostproc):
            # Already did module swap and registered args, early exit
            self._pipelined_postprocs.add(postproc_module)
            arg_info.add_step(ArgInfoStepFactory.postproc(postproc_module))
            return arg_info

        if not isinstance(postproc_module, torch.nn.Module):
            logger.warning(
                f"Expected postproc_module to be nn.Module but was {type(postproc_module)}"
            )
            return None

        # check if module is safe to pipeline i.e.no trainable param
        if not _check_postproc_pipelineable(postproc_module):
            return None

        # For module calls, `self` isn't counted
        total_num_args = len(child_node.args) + len(child_node.kwargs)
        if total_num_args == 0:
            # module call without any args, assume KJT modified
            return None

        # recursive call to check that all inputs to this postproc module
        # is either made of postproc module or non-modifying train batch input
        # transformations
        postproc_args, num_found_safe_postproc_args = self.get_node_args(
            child_node,
            for_postproc_module=True,
        )
        if num_found_safe_postproc_args == total_num_args:
            logger.info(
                f"""Module {postproc_module} is a valid postproc module (no
                trainable params and inputs can be derived from train batch input
                    via a series of either valid postproc modules or non-modifying
                    transformations) and will be applied during sparse data dist
                    stage"""
            )

            pipelined_postproc_module = PipelinedPostproc(
                postproc_module,
                postproc_module_fqn,
                postproc_args,
                self._context,
                default_stream=self._default_stream,
                dist_stream=self._dist_stream,
            )

            # module swap
            self._model = self._swap_postproc_module_recursive(
                self._model, pipelined_postproc_module, postproc_module_fqn
            )

            self._pipelined_postprocs.add(pipelined_postproc_module)
            arg_info.add_step(ArgInfoStepFactory.postproc(pipelined_postproc_module))
            return arg_info

        return None

    def _get_node_args_helper_inner(
        self,
        # pyre-ignore
        arg,
        for_postproc_module: bool = False,
    ) -> Optional[ArgInfo]:
        arg_info = ArgInfo([])
        while True:
            if not isinstance(arg, torch.fx.Node):
                return self._handle_constant(arg, arg_info, for_postproc_module)

            child_node = arg

            if child_node.op == "placeholder":
                return self._handle_placeholder(arg, arg_info)
            elif child_node.op == "call_module":
                return self._handle_module(arg, arg_info)
            elif (
                child_node.op == "call_function"
                and child_node.target.__module__ == "builtins"
                # pyre-fixme[16]
                and child_node.target.__name__ == "getattr"
            ):
                arg_info.add_step(
                    # pyre-fixme[6]: For 2nd argument expected `str` but got Unknown
                    ArgInfoStepFactory.get_attr(child_node.args[1])
                )
                arg = child_node.args[0]
            elif (
                child_node.op == "call_function"
                and child_node.target.__module__ == "_operator"
                # pyre-fixme[16]
                and child_node.target.__name__ == "getitem"
            ):
                arg_info.add_step(
                    # pyre-fixme[6]: For 2nd argument expected `str` but got Unknown
                    ArgInfoStepFactory.get_item(child_node.args[1])
                )
                arg = child_node.args[0]
            elif (
                child_node.op == "call_function"
                and child_node.target.__module__ == "torch.utils._pytree"
                # pyre-fixme[16]
                and child_node.target.__name__ == "tree_unflatten"
            ):
                """
                This is for the PT2 export path where we unflatten the input to reconstruct
                the structure with the recorded tree spec.
                """
                step = arg_info.steps[0]
                assert isinstance(step, GetItemArgInfoStep)
                # pyre-fixme[16]
                arg = child_node.args[0][step.item_index]
            elif (
                child_node.op == "call_function"
                and child_node.target.__module__ == "torchrec.sparse.jagged_tensor"
                # pyre-fixme[16]
                and child_node.target.__name__ == "KeyedJaggedTensor"
            ):
                call_module_found = False

                for arg_node in chain(child_node.args, child_node.kwargs.values()):
                    if isinstance(
                        arg_node, torch.fx.Node
                    ) and _check_args_for_call_module(arg_node):
                        call_module_found = True
                        break

                if call_module_found:
                    break

                if "values" in child_node.kwargs:
                    arg = child_node.kwargs["values"]
                else:
                    arg = child_node.args[1]

            elif child_node.op == "call_method" and child_node.target == "get":
                # pyre-ignore[6]
                arg_info.add_step(ArgInfoStepFactory.get_item(child_node.args[1]))
                arg = child_node.args[0]
            else:
                break

        # if we couldn't hit one of the "decisive" outcomes (constant, placeholder or module), return "not found"
        return None

    def _get_node_args_helper(
        self,
        # pyre-ignore
        arguments,
        # Add `None` constants to arg info only for postproc modules
        # Defaults to False for backward compatibility
        for_postproc_module: bool = False,
    ) -> Tuple[List[ArgInfo], int]:
        """
        Goes through the args/kwargs of a node and arranges them into a list of `ArgInfo`s.
        It also counts the number of (args + kwargs) found.
        """
        num_found = 0
        arg_info_list = []
        for arg in arguments:
            if not for_postproc_module and arg is None:
                arg_info = ArgInfo([ArgInfoStepFactory.from_scalar(None)])
                arg_info_list.append(arg_info)
                num_found += 1
                continue
            arg_info = self._get_node_args_helper_inner(
                arg,
                for_postproc_module,
            )
            if arg_info is not None:
                num_found += 1
                arg_info_list.append(arg_info)
        return arg_info_list, num_found

    def get_node_args(
        self,
        node: Node,
        for_postproc_module: bool = False,
    ) -> Tuple[CallArgs, int]:
        pos_arg_info_list, args_found = self._get_node_args_helper(
            node.args,
            for_postproc_module,
        )
        kwargs_arg_info_list, kwargs_found = self._get_node_args_helper(
            node.kwargs.values(),
            for_postproc_module,
        )

        # Replace with proper names for kwargs
        kwargs_info_list = dict(zip(node.kwargs, kwargs_arg_info_list))

        return CallArgs(pos_arg_info_list, kwargs_info_list), args_found + kwargs_found


def _get_leaf_module_names_helper(
    model: torch.nn.Module,
    path: str,
    leaf_module_names: Set[str],
) -> bool:
    """
    recursive function returns True if any of the sub-modules is ShardedModule.
    it also added the fqns of the sub-modules who do not contain any ShardedModule
    into the `leaf_module_names` unless it's marked as `_is_pytorch_fx_traceable = True`,
    which suggests this ShardedModule-free module should NOT be treated as a leaf module
    """
    sharded_children = set()
    for name, child in model.named_children():
        curr_path = path + name
        if isinstance(child, ShardedModule):
            sharded_children.add(name)
        else:
            child_sharded = _get_leaf_module_names_helper(
                child,
                curr_path + ".",
                leaf_module_names,
            )
            if child_sharded:
                sharded_children.add(name)

    # only do this for hybrid module (has sharded child)
    if len(sharded_children) > 0:
        for name, child in model.named_children():
            if name in sharded_children:
                continue
            # assume module is leaf node unless annotated otherwise
            if not getattr(child, "_is_pytorch_fx_traceable", False):
                leaf_module_names.add(path + name)
    return len(sharded_children) > 0


def _get_leaf_module_names(model: torch.nn.Module) -> List[str]:
    """
    Returns a list of top level modules to be used as leaf modules for FX tracing.
    This is a shallow FX trace that only goes the minimum depth required to pipeline.
    Any sub-module who does not contain a ShardedModule would be considered as a leaf
    module unless explicitly tagged as `_is_pytorch_fx_traceable = True`.
    """

    leaf_module_names: Set[str] = set()
    _get_leaf_module_names_helper(
        model,
        "",
        leaf_module_names,
    )
    return list(leaf_module_names)


def _jit_modules(module: torch.nn.Module, path: str, optional: bool = True) -> bool:
    sharded_children = set()
    for name, child in module.named_children():
        curr_path = path + name
        if isinstance(child, ShardedModule):
            sharded_children.add(name)
        else:
            child_sharded = _jit_modules(child, curr_path + ".", optional)
            if child_sharded:
                sharded_children.add(name)

    if len(sharded_children) > 0:
        for name, child in module.named_children():
            if name not in sharded_children:
                try:
                    jit_child = torch.jit.script(child)
                    setattr(module, name, jit_child)
                    logger.info(f"jit.script applied to {path + name}.")
                except Exception as error:
                    if not optional:
                        raise
                    else:
                        logger.info(
                            f"Warning: failed to jit.script {path + name}: {error}."
                        )

    return len(sharded_children) > 0


def _pipeline_detach_model(
    model: torch.nn.Module,
    pipelined_modules: List[ShardedModule],
    # pyre-ignore[2]
    original_forwards: List[Callable[..., Any]],
    original_kjt_dist_forwards: List[
        Callable[[KeyedJaggedTensor], Awaitable[KJTAllToAllTensorsAwaitable]]
    ],
    pipelined_postprocs: List[PipelinedPostproc],
) -> None:
    # Replace pipelined module forward and input dist forward with original forward
    kjt_dists = []
    for mod, original_fwd in zip(pipelined_modules, original_forwards):
        # pyre-ignore
        mod.forward = original_fwd

        for _, child_module in mod.named_modules():
            if not hasattr(child_module, "_input_dists"):
                continue
            for input_dist in child_module._input_dists:
                if hasattr(input_dist, "_dist"):
                    kjt_dists.append(input_dist._dist)
    assert len(kjt_dists) == len(
        original_kjt_dist_forwards
    ), f"Number of KJT dists ({len(kjt_dists)}) does not match number of kjt dist forwards provided ({len(original_kjt_dist_forwards)})"

    for kjt_dist, original_kjt_dist_fwd in zip(
        kjt_dists,
        original_kjt_dist_forwards,
    ):
        kjt_dist.forward = original_kjt_dist_fwd

    # Get underlying nn.Module
    if isinstance(model, DistributedModelParallel):
        model = model.module

    # Replace pipelined postproc modules with original postproc modules
    for postproc_mod in pipelined_postprocs:
        setattr(model, postproc_mod.fqn, postproc_mod.postproc_module)


# pyre-ignore[3] Return type must be specified as type that does not contain
def _rewrite_model(  # noqa C901
    model: torch.nn.Module,
    context: TForwardContext,
    dist_stream: Optional[torch.Stream],
    batch: Optional[In] = None,
    apply_jit: bool = False,
    pipelined_forward: Type[BaseForward[TrainPipelineContext]] = PipelinedForward,
    pipeline_postproc: bool = False,
    default_stream: Optional[torch.Stream] = None,
) -> Tuple[
    List[ShardedModule],
    torch.nn.Module,
    List[Callable[..., Any]],
    List[PipelinedPostproc],
    List[str],
]:
    """
    This is a very important util function used by TorchRec's sparse-dist (and others) train pipeline.

    The high-level idea of the sparse-dist train pipeline is to extract the forward calls of the sharded
    modules (e.g., ShardedEBC, ShardedEC, etc.) from the model's forward call, so that the sparse-dist
    pipeline can apply some optimization technique like overlapping the comms (i.e., input_dist) with
    compute (e.g., dense-forward, emb-lookup, etc.). And this "extraction of sharded forward" is done by
    this `_rewrite_model` util function.

    currently the `_rewrite_model` function uses fx tracer to capture the graph of the sharded model,
    and find the "call_module" nodes for sharded modules.

    theoretically the ShardedModule takes a KJT as the only input (EBC, EC, etc.), it calls `_get_node_args`
    to
    """
    input_model = model
    # Get underlying sharded model (nn.Module) from DistributedModelParallel
    #   which will not be wrapped in DDP, FSDP, DMP, or any other parallelism wrappers.
    if isinstance(model, DistributedModelParallel):
        model = model.module

    # Collect a list of sharded modules.
    sharded_modules: Dict[str, ShardedModule] = {}  # fqn -> ShardedModule
    for name, m in model.named_modules():
        if isinstance(m, ShardedModule):
            sharded_modules[name] = m

    ## Trace a model. for more: https://pytorch.org/docs/stable/fx.html
    concrete_args = {}
    """
    concrete_args allows you to partially specialize your function, whether it’s to remove
    control flow or data structures.
    """

    # special handling of placeholder, adding meta/label to the PH node
    if batch:
        if hasattr(batch, "to_proxy"):
            # for some special models, it requires using "input" as the key for input
            # pyre-ignore[16]: Variable[In (bound to Pipelineable)] has no attribute to_proxy.
            concrete_args["inputs"] = copy.copy(batch).to_proxy()
        elif hasattr(batch, "to_proxy_tuple"):
            # when the model is pre-fx traced or dynamo exported, the inputs are already flattened,
            # and therefore we use tuple as concrete args that fx.trace will automatically match
            # with the argument names. We pass in the model for the caller side to customize the batch
            # pyre-ignore[16]: Variable[In (bound to Pipelineable)] has no attribute to_proxy_tuple.
            concrete_args = batch.to_proxy_tuple(model)

    tracer = Tracer(leaf_modules=_get_leaf_module_names(model))
    graph = tracer.trace(model, concrete_args=concrete_args)

    # Select sharded modules, which are top-level in the forward call graph,
    # i.e. don't have input transformations, i.e. rely only on 'builtins.getattr'.
    pipelined_forwards = []
    original_forwards = []

    non_pipelined_sharded_modules = []

    args_helper = NodeArgsHelper(
        model, context, pipeline_postproc, default_stream, dist_stream
    )

    for node in graph.nodes:
        # only work on the call_module node which is also a sharded module
        if node.op != "call_module" or node.target not in sharded_modules:
            continue

        total_num_args = len(node.args) + len(node.kwargs)
        # only work on node with input(s), we don't expect zero input count for sharded module
        if total_num_args == 0:
            logger.warning(f"Module '{node.target}' is a ShardedModule with zero input")
            continue

        # List[ArgInfo]: for rebuilding the input arguments, while the num verifies if missing any
        arg_info_list, num_found = args_helper.get_node_args(node)

        if num_found == total_num_args:
            logger.info(f"Module '{node.target}' will be pipelined")
            child = sharded_modules[node.target]
            original_forwards.append(child.forward)
            # pyre-ignore[8] Incompatible attribute type
            child.forward = pipelined_forward(
                node.target,
                arg_info_list,
                child,
                context,
                dist_stream,
            )
            pipelined_forwards.append(child)
        else:
            logger.warning(
                f"Module '{node.target}' will NOT be pipelined, due to input modifications"
            )
            non_pipelined_sharded_modules.append(node.target)

    # JIT script unsharded modules if applicable.
    if apply_jit:
        graph_model = torch.fx.GraphModule(model, graph)
        _jit_modules(graph_model, "")
        if isinstance(input_model, DistributedModelParallel):
            input_model.module = graph_model

    if non_pipelined_sharded_modules:
        logger.warning(
            "Sharded modules were not pipelined: %s. "
            + "This should be fixed for pipelining to work to the full extent.",
            ", ".join(non_pipelined_sharded_modules),
        )

    return (
        pipelined_forwards,
        input_model,
        original_forwards,
        list(args_helper.pipelined_postprocs),
        non_pipelined_sharded_modules,
    )


def _override_input_dist_forwards(
    pipelined_modules: List[ShardedModule],
) -> List[Callable[[KeyedJaggedTensor], Awaitable[KJTAllToAllTensorsAwaitable]]]:
    """
    Overrides each input dist forward to support fusing the splits collective.
    NOTE: this can only be called after the input dists are initialized.
    """
    original_kjt_dist_forwards = []
    for module in pipelined_modules:
        for child_fqn, child_module in module.named_modules():
            if hasattr(child_module, "_has_uninitialized_input_dist"):
                assert (
                    not child_module._has_uninitialized_input_dist
                ), f"{child_fqn} has uninitialized input dist"

            if not hasattr(child_module, "_input_dists"):
                continue

            for input_dist in child_module._input_dists:
                if hasattr(input_dist, "_dist"):
                    assert isinstance(input_dist._dist, KJTAllToAll)
                    original_kjt_dist_forwards.append(input_dist._dist.forward)
                    input_dist._dist.forward = KJTAllToAllForward(
                        pg=input_dist._dist._pg,
                        splits=input_dist._dist._splits,
                        stagger=input_dist._dist._stagger,
                    )
    return original_kjt_dist_forwards


def get_h2d_func(batch: In, device: torch.device) -> Pipelineable:
    return batch.to(device, non_blocking=True)


class DataLoadingThread(Thread, Generic[In]):
    def __init__(
        self,
        device: torch.device,
        dataloader_iter: Iterator[In],
        to_device_non_blocking: bool,
        memcpy_stream_priority: int = 0,
        memcpy_stream: Optional[torch.Stream] = None,
    ) -> None:
        super().__init__(name="DataLoadingThread")
        self._stop: bool = False
        self.daemon = True  # Mark as daemon thread so that Python will not wait for it at shutdown.
        self._dataloader_iter = dataloader_iter
        self._buffer_empty_event: Event = Event()
        self._buffer_filled_event: Event = Event()
        if memcpy_stream is None:
            self._memcpy_stream: Optional[torch.Stream] = (
                torch.get_device_module(device).Stream(priority=memcpy_stream_priority)
                if device.type in ["cuda", "mtia"]
                else None
            )
        else:
            self._memcpy_stream = memcpy_stream
        self._device = device
        self._to_device_non_blocking = to_device_non_blocking
        self._buffered: Optional[In] = None
        self._buffer_empty_event.set()

    def run(self) -> None:
        if self._device.type == "cuda" and torch.cuda.is_available():
            # set the current device the same as the one used in the main thread
            torch.cuda.set_device(self._device)
        elif self._device.type == "mtia" and torch.mtia.is_available():
            # set the current device the same as the one used in the main thread
            torch.mtia.set_device(self._device)

        while not self._stop:
            self._buffer_empty_event.wait()
            # Set the filled event to unblock progress() and return.
            if self._stop:
                self._buffer_filled_event.set()
                return
            with record_function("## load_batch ##"):
                try:
                    batch = next(self._dataloader_iter)
                except StopIteration:
                    self._stop = True
                    self._buffer_filled_event.set()
                    return
            with record_function("## copy_batch_to_gpu ##"):
                with torch.get_device_module(self._device).stream(self._memcpy_stream):
                    self._buffered = cast(
                        In,
                        batch.to(
                            self._device, non_blocking=self._to_device_non_blocking
                        ),
                    )
                self._buffer_empty_event.clear()
                self._buffer_filled_event.set()

    def stop(self) -> None:
        logger.info("Stopping data loading thread...")
        self._stop = True
        # Unblock any thread that are waiting for these events.
        self._buffer_filled_event.set()
        self._buffer_empty_event.set()
        logger.info("Data loading thread stopped.")

    def get_next_batch(self, none_throws: bool = False) -> Optional[In]:
        """
        Get the next batch from the buffer if threading is enabled, otherwise
        call load_next_batch directly.

        This function is not thread safe. We assume this is only invoked from
        the main thread in the training loop.
        """
        self._buffer_filled_event.wait()
        batch = self._buffered
        if batch is None:
            if none_throws:
                raise StopIteration
            return None
        self._buffered = None
        self._buffer_filled_event.clear()
        self._buffer_empty_event.set()
        return batch


def _prefetch_embeddings(
    batch: In,
    context: PrefetchTrainPipelineContext,
    pipelined_modules: List[ShardedModule],
    device: torch.device,
    stream_context: Callable[[Optional[torch.Stream]], torch.cuda.StreamContext],
    data_dist_stream: Optional[torch.Stream],
    default_stream: Optional[torch.Stream],
) -> Dict[str, KJTList]:
    data_per_sharded_module = {}
    for sharded_module in pipelined_modules:
        forward = sharded_module.forward
        assert isinstance(forward, PrefetchPipelinedForward)
        assert forward._name in context.input_dist_tensors_requests
        request = context.input_dist_tensors_requests.pop(forward._name)
        assert isinstance(request, Awaitable)
        with record_function(f"## _prefetch_embeddings {context.index} ##"):
            # Finish waiting on the dist_stream,
            # in case some delayed stream scheduling happens during the wait() call.
            with stream_context(data_dist_stream):
                data = request.wait()

        # Make sure that both result of input_dist and context
        # are properly transferred to the current stream.
        module_context = context.module_contexts[forward._name]
        if data_dist_stream is not None:
            torch.get_device_module(device).current_stream().wait_stream(
                data_dist_stream
            )
            cur_stream = torch.get_device_module(device).current_stream()

            assert isinstance(
                data, (torch.Tensor, Multistreamable)
            ), f"{type(data)} must implement Multistreamable interface"
            data.record_stream(cur_stream)
            if default_stream:
                data.record_stream(default_stream)

            module_context.record_stream(cur_stream)
            if default_stream:
                module_context.record_stream(default_stream)

        sharded_module.prefetch(
            ctx=module_context,
            dist_input=data,
            forward_stream=default_stream,
        )
        data_per_sharded_module[forward._name] = data
    return data_per_sharded_module


@contextlib.contextmanager
def use_context_for_postprocs(
    pipelined_postprocs: List[PipelinedPostproc],
    next_batch_context: TrainPipelineContext,
) -> Generator[None, None, None]:
    """
    Temporarily set pipelined postproc context for next iter to populate cache.
    """
    # Save original context for model fwd
    original_contexts = [p.get_context() for p in pipelined_postprocs]

    # Temporarily set context for next iter to populate cache
    for postproc_mod in pipelined_postprocs:
        postproc_mod.set_context(next_batch_context)

    yield

    # Restore context for model fwd
    for module, context in zip(pipelined_postprocs, original_contexts):
        module.set_context(context)


class SparseDataDistUtil(Generic[In]):
    """
    Helper class exposing methods for sparse data dist and prefetch pipelining.
    Currently used for `StagedTrainPipeline` pipeline stages

    Args:
        model (torch.nn.Module): Model to pipeline
        data_dist_stream (torch.cuda.Stream): Stream on which to run sparse data dist.
        apply_jit (bool): apply torch.jit.script to non-pipelined (unsharded) modules.
        prefetch_stream (Optional[torch.cuda.Stream]): Stream on which model prefetch runs
            Defaults to `None`. This needs to be passed in to enable prefetch pipelining.
        pipeline_postproc (bool): whether to pipeline postproc modules. Defaults to `False`.

    Example::
        sdd = SparseDataDistUtil(
            model=model,
            data_dist_stream=torch.cuda.Stream(),
            prefetch_stream=torch.cuda.Stream(), <-- required to enable prefetch pipeline
        )
        pipeline = [
            PipelineStage(
                name="data_copy",
                runnable=lambda batch, context: batch.to(
                    self._device, non_blocking=True
                ),
                stream=torch.cuda.Stream(),
            ),
            PipelineStage(
                name="start_sparse_data_dist",
                runnable=sdd.start_sparse_data_dist,
                stream=sdd.data_dist_stream,
                fill_callback=sdd.wait_sdd_fill_callback,
            ),
            PipelineStage(
                name="prefetch",
                runnable=sdd.prefetch,
                stream=sdd.prefetch_stream,
                fill_callback=sdd.load_prefetch,
            ),
        ]

        return StagedTrainPipeline(pipeline_stages=pipeline)
    """

    _TRAIN_CONTEXT_VERSION = 1
    # Convenience flag to perform additional assertions on contexts
    # to make sure contexts are advancing correctly.
    _WITH_CONTEXT_ASSERTIONS = False

    def __init__(
        self,
        model: torch.nn.Module,
        data_dist_stream: torch.Stream,
        apply_jit: bool = False,
        prefetch_stream: Optional[torch.Stream] = None,
        pipeline_postproc: bool = False,
    ) -> None:
        super().__init__()
        self.model = model
        self.data_dist_stream = data_dist_stream
        self.apply_jit = apply_jit
        self.prefetch_stream = prefetch_stream
        self._next_index: int = 0
        self._contexts: Deque[TrainPipelineContext] = deque()
        self.initialized = False
        self._pipelined_modules: List[ShardedModule] = []
        self._pipelined_postprocs: List[PipelinedPostproc] = []
        self.fwd_hook: Optional[RemovableHandle] = None
        self._device: torch.device = data_dist_stream.device

        self._stream_context: Callable[
            [Optional[torch.Stream]], torch.cuda.StreamContext
        ] = (
            torch.get_device_module(self._device).stream
            if self._device.type in ["cuda", "mtia"]
            else torch.cuda.stream
        )

        # pyre-ignore
        self._original_forwards: List[Callable[..., Any]] = []
        self._original_kjt_dist_forwards: List[
            Callable[[KeyedJaggedTensor], Awaitable[KJTAllToAllTensorsAwaitable]]
        ] = []

        self._pipelined_forward: Type[BaseForward[TrainPipelineContext]] = cast(
            Type[BaseForward[TrainPipelineContext]],
            (PrefetchPipelinedForward if self._with_prefetch else PipelinedForward),
        )

        self._default_stream: Optional[torch.Stream] = (
            (torch.get_device_module(self._device).Stream())
            if self._device.type in ["cuda", "mtia"]
            else None
        )
        # When data iterator is exhausted, contexts should continue advancing until
        # reaching the end (i.e. no longer called from the StagedTrainingPipeline)
        # however normal invariants no longer apply (e.g. module_contexts might be empty
        # before prefetch stage). Currently, all actions (`prefetch`, `start/wait_sparse_data_dist`)
        # tolerate lack of data from the previous stage - so context assertions are mostly
        # correctness invariant. However, if that changes, having invariants monitored/enforced
        # during exhastion phase might become necessary.
        self._exhausting_mode = False
        self._pipeline_postproc = pipeline_postproc

    @property
    def _with_prefetch(self) -> bool:
        return self.prefetch_stream is not None

    def _is_reattaching(self) -> bool:
        return len(self._contexts) > 0

    def should_assert_context_invariants(self, ctx: TrainPipelineContext) -> bool:
        return (
            self._WITH_CONTEXT_ASSERTIONS
            and self.initialized
            and not self._exhausting_mode
            and (
                ctx.index is not None and ctx.index >= 0
            )  # "fake contexts" to support pipeline initialization
        )

    # === Debugging helpers === #
    @property
    def _have_pipelined_modules(self) -> bool:
        return len(self._pipelined_modules) > 0

    @property
    def _have_pipelined_postprocs(self) -> bool:
        return len(self._pipelined_postprocs) > 0

    def _pipelined_modules_fqns(self) -> Set[str]:
        return {module.forward._name for module in self._pipelined_modules}

    def _pipelined_postprocs_fqns(self) -> Set[str]:
        return {module._fqn for module in self._pipelined_postprocs}

    # === Debugging helpers === #

    # ==== Context management === #
    # In short: version=1 contexts essentially represent "passing of time"
    # and have one-to-one correspondence to batches. "Monolithic" torchrec pipelines
    # (e.g. TrainPipelineSparseDist) explicitly manage batches and contexts together
    # (see TrainPipelineSparseDist.enqueue_batch), however StagedTrainPipeline abstracts
    # that away + supports stages that don't require contexts (in fact, SDD is the only one)
    # So we just manage contexts and batches together in lockstep - via _advance_context calls.
    #
    # Essentially, StagedTrainPipeline during a single `progress` call runs each stage
    # for a different batch, keeping the stage outputs in a `_stage_outputs` list, and
    # advancing the list at the beginning of the `progress`.
    # Tricky part is that SparseDataDistUtil might be participating in TWO stages:
    # * "main" with start_data_dist -> wait_data_dist pair for `runnable` and `fill_callback`
    # * "prefetch" with prefetch -> load_prefetch for `runnable` and `fill_callback`
    #
    # For this to work, we:
    # (1) need to manage contexts in a lockstep with batch advancing through stages (_advance_context)
    # (2) perform various actions (start dist, wait dist, etc.) against the correct contexts
    #    ("named" contexts below and how they are used in start/wait sparse_dist, prefetch, etc.)
    # (3) set contexts for the _pipelined_modules and _pipelined_postprocs to the "current batch context"
    #       for the model to run correctly (_set_module_context)
    #
    # SDD Util uses two or three contexts, depending on if prefetch is present
    # * context[0] is always the "current batch" context - used for model forward (outside this class)
    # * context[1] is used for prefetch if it is set, and start/wait_sparse_data_dist if not
    # * context[2] is used for start/wait_sparse_data_dist if prefetch is not set

    def _create_context(self, index: int) -> TrainPipelineContext:
        version = self._TRAIN_CONTEXT_VERSION
        return (
            PrefetchTrainPipelineContext(index=index, version=version)
            if self._with_prefetch
            else TrainPipelineContext(index=index, version=version)
        )

    def _add_context(self) -> None:
        self._contexts.append(self._create_context(self._next_index))
        self._next_index += 1

    def _advance_context(self) -> None:
        self._assert_contexts_count()
        self._contexts.popleft()
        self._add_context()
        self._set_module_context(self._context_for_model_forward())

    def _set_module_context(self, context: TrainPipelineContext) -> None:
        for module in self._pipelined_modules:
            module.forward.set_context(context)

        for postproc_module in self._pipelined_postprocs:
            # This ensures that next iter model fwd uses cached results
            postproc_module.set_context(context)

    def _assert_contexts_count(self) -> None:
        if not self._WITH_CONTEXT_ASSERTIONS:
            return
        contexts_len = len(self._contexts)
        expected = 3 if self._with_prefetch else 2
        assert (
            contexts_len == expected
        ), f"Expected to have {expected} contexts, but had {contexts_len}"

    # ====== "Named" contexts - to make it clearer which contexts are used for which operation ====== #
    # This is purely convenience methods, feel free to remove if they get in the way
    def _current_context(self) -> TrainPipelineContext:
        return self._contexts[0]

    def _assert_input_dist_tensors(
        self, context: TrainPipelineContext, expected_fqns: Set[str]
    ) -> None:
        specified_keys = context.input_dist_tensors_requests.keys()
        assert (
            specified_keys == expected_fqns
        ), f"Context(idx:{context.index}).input_dist_tensors_requests {specified_keys} != pipelined modules fqns {expected_fqns}"

    def _assert_module_contexts(
        self, context: TrainPipelineContext, expected_fqns: Set[str]
    ) -> None:
        specified_keys = context.module_contexts.keys()
        assert (
            specified_keys == expected_fqns
        ), f"Context(idx:{context.index}).module_contexts {specified_keys} != pipelined modules fqns {expected_fqns}"

    def _assert_module_contexts_post_prefetch(
        self, context: PrefetchTrainPipelineContext, expected_fqns: Set[str]
    ) -> None:
        specified_keys = context.module_contexts_post_prefetch.keys()
        assert (
            specified_keys == expected_fqns
        ), f"Context(idx:{context.index}).module_contexts_post_prefetch {specified_keys} != pipelined modules fqns {expected_fqns}"

    def _assert_module_input_post_prefetch(
        self, context: PrefetchTrainPipelineContext, expected_fqns: Set[str]
    ) -> None:
        specified_keys = context.module_input_post_prefetch.keys()
        assert (
            specified_keys == expected_fqns
        ), f"Context(idx:{context.index}).module_input_post_prefetch {specified_keys} != pipelined modules fqns {expected_fqns}"

    def _context_for_model_forward(self) -> TrainPipelineContext:
        ctx = self._current_context()
        if self.should_assert_context_invariants(ctx):
            target_fqns = self._pipelined_modules_fqns()
            if self._with_prefetch:
                assert isinstance(ctx, PrefetchTrainPipelineContext)
                self._assert_module_input_post_prefetch(ctx, target_fqns)
                self._assert_module_contexts_post_prefetch(ctx, target_fqns)
            else:
                self._assert_input_dist_tensors(ctx, target_fqns)
                self._assert_module_contexts(ctx, target_fqns)
        return ctx

    def _start_dist_context(self) -> TrainPipelineContext:
        if self._with_prefetch:
            ctx = self._contexts[2]
        else:
            ctx = self._contexts[1]

        return ctx

    def _wait_dist_context(self) -> TrainPipelineContext:
        # Note: see comment on the forward_hook in _initialize method
        ctx = self._start_dist_context()
        if self.should_assert_context_invariants(ctx):
            if self._have_pipelined_modules:
                assert (
                    len(ctx.fused_splits_awaitables) > 0
                ), f"fused_splits_awaitables was empty on {ctx.index=} - was start_sparse_data_dist called?"
        return ctx

    def _prefetch_context(self) -> PrefetchTrainPipelineContext:
        ctx = self._contexts[1]
        assert isinstance(
            ctx, PrefetchTrainPipelineContext
        ), "Pass prefetch_stream into SparseDataDistUtil to use prefetch_context()"
        if self.should_assert_context_invariants(ctx):
            target_fqns = self._pipelined_modules_fqns()
            self._assert_input_dist_tensors(ctx, target_fqns)
            self._assert_module_contexts(ctx, target_fqns)
        return ctx

    # ====== End "Named" contexts ====== #

    # === End context management === #

    def detach(self) -> torch.nn.Module:
        """
        Removes sparse data dist (SDD) pipelining from model forward and input dist.
        Modifies existing model in place and returns the model.

        detach() can be called at any point, and inflight batches do not need to be
        flushed before calling it. Calling pipeline.progress() will re-attach the model
        to the pipeline and the pipeline will progress normally from the point it was
        detached (i.e. inflight batches will be kept when calling detach).

        While the model is detached, it is equivalent to the model before passing to
        the pipeline, so forward and backward passes, and optimizer updates can be
        carried out normally.
        """
        if self.initialized:
            assert self.fwd_hook is not None
            self.fwd_hook.remove()

            _pipeline_detach_model(
                model=self.model,
                pipelined_modules=self._pipelined_modules,
                original_forwards=self._original_forwards,
                original_kjt_dist_forwards=self._original_kjt_dist_forwards,
                pipelined_postprocs=self._pipelined_postprocs,
            )

        self.initialized = False
        return self.model

    def _initialize_or_reattach(self, batch: In) -> None:
        # Step 0: Handle differences between initialization and reattaching
        if self._is_reattaching():
            # if reattaching, contexts are already there, so we want to use
            # the current context for model forward - as if continuing to run normally
            context_for_rewrite = self._current_context()
        else:
            # if initializing, no contexts are present, so we add them:
            if self._with_prefetch:
                self._contexts.append(self._create_context(-2))  # throwaway context
            self._contexts.append(self._create_context(-1))  # throwaway context
            self._add_context()  # actual context to be used for everything in the initial iteration
            context_for_rewrite = self._contexts[-1]

        self._assert_contexts_count()

        # Step 1: Pipeline input dist in trec sharded modules
        (
            self._pipelined_modules,
            self.model,
            self._original_forwards,
            self._pipelined_postprocs,
            _,
        ) = _rewrite_model(
            model=self.model,
            context=context_for_rewrite,
            dist_stream=self.data_dist_stream,
            batch=batch,
            apply_jit=self.apply_jit,
            pipelined_forward=self._pipelined_forward,
            pipeline_postproc=self._pipeline_postproc,
            default_stream=self._default_stream,
        )
        # Setting the stage for the first batch
        # initialize input dist
        _start_data_dist(self._pipelined_modules, batch, self._start_dist_context())
        # so we can override input dist forwards
        self._original_kjt_dist_forwards = _override_input_dist_forwards(
            self._pipelined_modules
        )

        # Step 2: Register post-forward hook to wait SDD and advance contexts
        def forward_hook(
            module: torch.nn.Module,
            input: Union[torch.Tensor, Tuple[torch.Tensor]],
            output: Union[torch.Tensor, Tuple[torch.Tensor]],
        ) -> None:
            # Note: tricky part - a bit delicate choreography between
            # StagedPipeline and this class
            # (see https://github.com/pytorch/torchrec/pull/2239 for details)
            # wait_dist need to be called as post_forward hook
            # at the end of the batch N, so that the data is awaited
            # before start of the next batch.
            self.wait_sparse_data_dist()
            # _advance_context should be called after wait_sparse_data_dist,
            # but before start_data_dist for the next batch
            # which means right here, and nowhere else
            self._advance_context()
            # ... this can be made more explicit by adding dedicated hooks for "batch start"/"batch end" events
            # to the StagedPipeline, PipelineStage and this class, but hook seems to be doing an adequate job for now

        self.fwd_hook = self.model.register_forward_hook(forward_hook)

        self.initialized = True

    def wait_sdd_fill_callback(self) -> None:
        """
        Used by StagedTrainPipeline during only during initial pipeline filling.

        At that part, model.forward is not executed, so forward hook is not called.
        """
        self.wait_sparse_data_dist()
        self._advance_context()

    def data_exhausted_callback(self) -> None:
        """
        Called by StagedTrainPipeline when all batches were processed.
        """
        self._exhausting_mode = True

    def start_sparse_data_dist(self, batch: In) -> In:
        if not self.initialized:
            self._initialize_or_reattach(batch)

        ctx = self._start_dist_context()
        with record_function(f"## start_sparse_data_dist {ctx.index} ##"):
            with use_context_for_postprocs(self._pipelined_postprocs, ctx):
                _start_data_dist(self._pipelined_modules, batch, ctx)

        return batch

    def wait_sparse_data_dist(self) -> None:
        """
        Waits on the input dist splits requests to get the input dist tensors requests,
        and populates the context with them.
        """
        ctx = self._wait_dist_context()
        with record_function(f"## wait_sparse_data_dist {ctx.index} ##"):
            with self._stream_context(self.data_dist_stream):
                for names, awaitable in ctx.fused_splits_awaitables:
                    for name, request in zip(names, awaitable.wait()):
                        ctx.input_dist_tensors_requests[name] = request
        # these won't be used by the rest of the pipeline, so just deleting them to free
        # the memory they occupy
        ctx.input_dist_splits_requests.clear()
        ctx.fused_splits_awaitables.clear()

    def prefetch(self, batch: In) -> In:
        """
        Waits for input dist to finish, then prefetches data.
        """
        assert isinstance(
            self._prefetch_context(), PrefetchTrainPipelineContext
        ), "Pass prefetch_stream into SparseDataDistUtil to use prefetch() as a stage"
        ctx: PrefetchTrainPipelineContext = self._prefetch_context()

        with self._stream_context(self.prefetch_stream):
            data_per_pipelined_module = _prefetch_embeddings(
                batch,
                ctx,
                self._pipelined_modules,
                self._device,
                self._stream_context,
                self.data_dist_stream,
                self._default_stream,
            )
            # TODO (eugenykolpakov): investigate if these can be moved outside of the `with stream_context(...)`  block
            # This might impact memory fragmentation (since CUDA caching allocator is stream-aware),
            # so need to check how memory behaves with different streams
            for sharded_module in self._pipelined_modules:
                forward = sharded_module.forward
                data = data_per_pipelined_module[forward._name]
                ctx.module_input_post_prefetch[forward._name] = data
                ctx.module_contexts_post_prefetch[forward._name] = (
                    ctx.module_contexts.pop(forward._name)
                )
        return batch

    def load_prefetch(self) -> None:
        """
        DEPRECATED: exists for backward compatibility
        """
        # Version=0 did
        # module_input_post_prefetch = module_input_post_prefetch_for_next_batch
        # module_contexts_post_prefetch = module_contexts_post_prefetch_for_next_batch
        # with version=1, there's nothing to do - they are managed at a context level,
        # so this is essentially done by _advance_context + prefetch above
        pass
