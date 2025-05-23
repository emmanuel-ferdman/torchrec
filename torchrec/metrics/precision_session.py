#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import logging
from typing import Any, cast, Dict, List, Optional, Set, Type, Union

import torch
from torch import distributed as dist
from torchrec.metrics.metrics_config import RecTaskInfo, SessionMetricDef
from torchrec.metrics.metrics_namespace import MetricName, MetricNamespace, MetricPrefix
from torchrec.metrics.rec_metric import (
    MetricComputationReport,
    RecComputeMode,
    RecMetric,
    RecMetricComputation,
    RecMetricException,
)
from torchrec.metrics.recall_session import (
    _calc_num_true_pos,
    _validate_model_outputs,
    ranking_within_session,
)

logger: logging.Logger = logging.getLogger(__name__)

NUM_TRUE_POS = "num_true_pos"
NUM_FALSE_POS = "num_false_pos"


def _calc_num_false_pos(
    labels: torch.Tensor, predictions: torch.Tensor, weights: torch.Tensor
) -> torch.Tensor:
    # predictions are expected to be 0 or 1 integers.
    num_false_pos = torch.sum(
        weights * (1 - labels) * (predictions == 1).double(), dim=-1
    )
    return num_false_pos


def _calc_precision(
    num_true_pos: torch.Tensor, num_false_pos: torch.Tensor
) -> torch.Tensor:
    # if num_true_pos + num_false_pos == 0 then we set precision = NaN by default.
    precision = torch.tensor([float("nan")])
    if (num_true_pos + num_false_pos).item() != 0:
        precision = num_true_pos / (num_true_pos + num_false_pos)
    else:
        logger.warning(
            "precision = NaN. Likely, it means that there were no positive predictions passed to the metric yet."
            " Please, debug if you expect every batch to include positive predictions."
        )
    return precision


class PrecisionSessionMetricComputation(RecMetricComputation):
    r"""
    This class implements the RecMetricComputation for precision on session level.

    The constructor arguments are defined in RecMetricComputation.
    See the docstring of RecMetricComputation for more detail.

    """

    def __init__(
        self,
        *args: Any,
        session_metric_def: SessionMetricDef,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._add_state(
            NUM_TRUE_POS,
            torch.zeros(self._n_tasks, dtype=torch.double),
            add_window_state=True,
            dist_reduce_fx="sum",
            persistent=True,
        )
        self._add_state(
            NUM_FALSE_POS,
            torch.zeros(self._n_tasks, dtype=torch.double),
            add_window_state=True,
            dist_reduce_fx="sum",
            persistent=True,
        )
        self.top_threshold: Optional[int] = session_metric_def.top_threshold
        self.run_ranking_of_labels: bool = session_metric_def.run_ranking_of_labels
        self.session_var_name: Optional[str] = session_metric_def.session_var_name

    def update(
        self,
        *,
        predictions: Optional[torch.Tensor],
        labels: torch.Tensor,
        weights: Optional[torch.Tensor],
        **kwargs: Dict[str, Any],
    ) -> None:
        """
        Args:
            predictions (torch.Tensor): tensor of size (n_task, n_examples)
            labels (torch.Tensor): tensor of size (n_task, n_examples)
            weights (torch.Tensor): tensor of size (n_task, n_examples)
            session (torch.Tensor): Optional tensor of size (n_task, n_examples) that specifies the groups of
                    predictions/labels per batch.
        """

        if (
            "required_inputs" not in kwargs
            or self.session_var_name not in kwargs["required_inputs"]
        ):
            raise RecMetricException(
                "Need the {} input to update the session metric".format(
                    self.session_var_name
                )
            )
        # pyre-ignore
        session = kwargs["required_inputs"][self.session_var_name]
        if predictions is None or weights is None or session is None:
            raise RecMetricException(
                "Inputs 'predictions', 'weights' and 'session' should not be None for PrecisionSessionMetricComputation update"
            )
        _validate_model_outputs(labels, predictions, weights, session)

        predictions = predictions.double()
        labels = labels.double()
        weights = weights.double()

        num_samples = predictions.shape[-1]
        for state_name, state_value in self.get_precision_states(
            labels=labels, predictions=predictions, weights=weights, session=session
        ).items():
            state = getattr(self, state_name)
            state += state_value
            self._aggregate_window_state(state_name, state_value, num_samples)

    def _compute(self) -> List[MetricComputationReport]:
        return [
            MetricComputationReport(
                name=MetricName.PRECISION_SESSION_LEVEL,
                metric_prefix=MetricPrefix.LIFETIME,
                value=_calc_precision(
                    num_true_pos=cast(torch.Tensor, getattr(self, NUM_TRUE_POS)),
                    num_false_pos=cast(torch.Tensor, getattr(self, NUM_FALSE_POS)),
                ),
            ),
            MetricComputationReport(
                name=MetricName.PRECISION_SESSION_LEVEL,
                metric_prefix=MetricPrefix.WINDOW,
                value=_calc_precision(
                    num_true_pos=self.get_window_state(NUM_TRUE_POS),
                    num_false_pos=self.get_window_state(NUM_FALSE_POS),
                ),
            ),
        ]

    def get_precision_states(
        self,
        labels: torch.Tensor,
        predictions: torch.Tensor,
        weights: torch.Tensor,
        session: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        predictions_ranked = ranking_within_session(predictions, session)
        # pyre-fixme[58]: `<` is not supported for operand types `Tensor` and
        #  `Optional[int]`.
        predictions_labels = (predictions_ranked < self.top_threshold).to(torch.int32)
        if self.run_ranking_of_labels:
            labels_ranked = ranking_within_session(labels, session)
            # pyre-fixme[58]: `<` is not supported for operand types `Tensor` and
            #  `Optional[int]`.
            labels = (labels_ranked < self.top_threshold).to(torch.int32)
        num_true_pos = _calc_num_true_pos(labels, predictions_labels, weights)
        num_false_pos = _calc_num_false_pos(labels, predictions_labels, weights)

        return {NUM_TRUE_POS: num_true_pos, NUM_FALSE_POS: num_false_pos}


class PrecisionSessionMetric(RecMetric):
    _namespace: MetricNamespace = MetricNamespace.PRECISION_SESSION_LEVEL
    _computation_class: Type[RecMetricComputation] = PrecisionSessionMetricComputation

    def __init__(
        self,
        world_size: int,
        my_rank: int,
        batch_size: int,
        tasks: List[RecTaskInfo],
        compute_mode: RecComputeMode = RecComputeMode.UNFUSED_TASKS_COMPUTATION,
        window_size: int = 100,
        fused_update_limit: int = 0,
        process_group: Optional[dist.ProcessGroup] = None,
        **kwargs: Any,
    ) -> None:
        if compute_mode in [
            RecComputeMode.FUSED_TASKS_COMPUTATION,
            RecComputeMode.FUSED_TASKS_AND_STATES_COMPUTATION,
        ]:
            raise RecMetricException(
                "Fused computation is not supported for precision session-level metrics"
            )

        if fused_update_limit > 0:
            raise RecMetricException(
                "Fused update is not supported for precision session-level metrics"
            )
        for task in tasks:
            if task.session_metric_def is None:
                raise RecMetricException(
                    "Please, specify the session metric definition"
                )
            session_metric_def = task.session_metric_def
            if session_metric_def.top_threshold is None:
                raise RecMetricException("Please, specify the top threshold")

        super().__init__(
            world_size=world_size,
            my_rank=my_rank,
            batch_size=batch_size,
            tasks=tasks,
            compute_mode=compute_mode,
            window_size=window_size,
            fused_update_limit=fused_update_limit,
            process_group=process_group,
            **kwargs,
        )

    def _get_task_kwargs(
        self, task_config: Union[RecTaskInfo, List[RecTaskInfo]]
    ) -> Dict[str, Any]:
        if isinstance(task_config, list):
            raise RecMetricException("Session metric can only take one task at a time")

        if task_config.session_metric_def is None:
            raise RecMetricException("Please, specify the session metric definition")

        return {"session_metric_def": task_config.session_metric_def}

    def _get_task_required_inputs(
        self, task_config: Union[RecTaskInfo, List[RecTaskInfo]]
    ) -> Set[str]:
        if isinstance(task_config, list):
            raise RecMetricException("Session metric can only take one task at a time")

        if task_config.session_metric_def is None:
            raise RecMetricException("Please, specify the session metric definition")

        return (
            {task_config.session_metric_def.session_var_name}
            if task_config.session_metric_def.session_var_name
            else set()
        )
