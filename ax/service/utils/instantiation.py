#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.

from typing import Dict, List, Optional, Union, cast

import numpy as np
from ax.core.arm import Arm
from ax.core.data import Data
from ax.core.experiment import Experiment
from ax.core.metric import Metric
from ax.core.objective import Objective
from ax.core.optimization_config import OptimizationConfig
from ax.core.outcome_constraint import OutcomeConstraint
from ax.core.parameter import (
    PARAMETER_PYTHON_TYPE_MAP,
    ChoiceParameter,
    FixedParameter,
    Parameter,
    ParameterType,
    RangeParameter,
    TParameterType,
)
from ax.core.parameter_constraint import (
    OrderConstraint,
    ParameterConstraint,
    SumConstraint,
)
from ax.core.search_space import SearchSpace
from ax.core.simple_experiment import DEFAULT_OBJECTIVE_NAME
from ax.core.types import (
    ComparisonOp,
    TEvaluationOutcome,
    TFidelityTrialEvaluation,
    TParameterization,
    TParamValue,
    TTrialEvaluation,
)
from ax.utils.common.typeutils import not_none, numpy_type_to_python_type


"""Utilities for RESTful-like instantiation of Ax classes needed in AxClient."""


TParameterRepresentation = Dict[str, Union[TParamValue, List[TParamValue]]]
PARAM_CLASSES = ["range", "choice", "fixed"]
PARAM_TYPES = {"int": int, "float": float, "bool": bool, "str": str}
COMPARISON_OPS = {"<=": ComparisonOp.LEQ, ">=": ComparisonOp.GEQ}


def _get_parameter_type(python_type: TParameterType) -> ParameterType:
    for param_type, py_type in PARAMETER_PYTHON_TYPE_MAP.items():
        if py_type is python_type:
            return param_type
    raise ValueError(f"No AE parameter type corresponding to {python_type}.")


def _to_parameter_type(
    vals: List[TParamValue], typ: Optional[str], param_name: str, field_name: str
) -> ParameterType:
    if typ is None:
        typ = type(not_none(vals[0]))
        parameter_type = _get_parameter_type(typ)  # pyre-ignore[6]
        assert all(isinstance(x, typ) for x in vals), (
            f"Values in `{field_name}` not of the same type and no `value_type` was "
            f"explicitly specified; cannot infer value type for parameter {param_name}."
        )
        return parameter_type
    return _get_parameter_type(PARAM_TYPES[typ])  # pyre-ignore[6]


def _make_range_param(
    name: str, representation: TParameterRepresentation, parameter_type: Optional[str]
) -> RangeParameter:
    assert "bounds" in representation, "Bounds are required for range parameters."
    bounds = representation["bounds"]
    assert isinstance(bounds, list) and len(bounds) == 2, (
        f"Cannot parse parameter {name}: for range parameters, json representation "
        "should include a list of two values, lower and upper bounds of the bounds."
    )
    return RangeParameter(
        name=name,
        parameter_type=_to_parameter_type(bounds, parameter_type, name, "bounds"),
        # pyre-fixme[6]: Expected `float` for 3rd param but got
        #  `Optional[Union[bool, float, int, str]]`.
        lower=bounds[0],
        upper=bounds[1],
        log_scale=representation.get("log_scale", False),
        is_fidelity=representation.get("is_fidelity", False),
    )


def _make_choice_param(
    name: str, representation: TParameterRepresentation, parameter_type: Optional[str]
) -> ChoiceParameter:
    assert "values" in representation, "Values are required for choice parameters."
    values = representation["values"]
    assert isinstance(values, list) and len(values) > 1, (
        f"Cannot parse parameter {name}: for choice parameters, json representation"
        " should include a list values, lower and upper bounds of the range."
    )
    return ChoiceParameter(
        name=name,
        parameter_type=_to_parameter_type(values, parameter_type, name, "values"),
        values=values,
        # pyre-fixme[6]: Expected `bool` for 4th param but got
        #  `Optional[Union[List[Optional[Union[bool, float, int, str]]], bool, float,
        #  int, str]]`.
        is_ordered=representation.get("is_ordered", False),
    )


def _make_fixed_param(
    name: str, representation: Dict[str, TParamValue], parameter_type: Optional[str]
) -> FixedParameter:
    assert "value" in representation, "Value is required for fixed parameters."
    value = representation["value"]
    assert type(value) in PARAM_TYPES.values(), (
        f"Cannot parse fixed parameter {name}: for fixed parameters, json "
        "representation should include a single value."
    )
    return FixedParameter(
        name=name,
        parameter_type=_get_parameter_type(type(value))  # pyre-ignore[6]
        if parameter_type is None
        else _get_parameter_type(PARAM_TYPES[parameter_type]),  # pyre-ignore[6]
        value=value,
    )


def parameter_from_json(
    representation: Dict[str, Union[TParamValue, List[TParamValue]]]
) -> Parameter:
    """Instantiate a parameter from JSON representation."""
    name = representation["name"]
    assert isinstance(name, str), "Parameter name must be a string."
    parameter_class = representation["type"]
    assert (
        isinstance(parameter_class, str) and parameter_class in PARAM_CLASSES
    ), "Type in parameter JSON representation must be `range`, `choice`, or `fixed`."

    parameter_type = representation.get("value_type", None)
    if parameter_type is not None:
        assert isinstance(parameter_type, str) and parameter_type in PARAM_TYPES, (
            "Value type in parameter JSON representation must be 'int', 'float', "
            "'bool' or 'str'."
        )

    if parameter_class == "range":
        return _make_range_param(
            name=name, representation=representation, parameter_type=parameter_type
        )

    if parameter_class == "choice":
        return _make_choice_param(
            name=name, representation=representation, parameter_type=parameter_type
        )

    if parameter_class == "fixed":
        assert not any(isinstance(val, list) for val in representation.values())
        return _make_fixed_param(
            name=name,
            representation=cast(Dict[str, TParamValue], representation),
            parameter_type=parameter_type,
        )
    else:
        raise ValueError(  # pragma: no cover (this is unreachable)
            f"Unrecognized parameter type {parameter_class}."
        )


def constraint_from_str(
    representation: str, parameters: Dict[str, Parameter]
) -> ParameterConstraint:
    """Parse string representation of a parameter constraint."""
    tokens = representation.split()
    parameter_names = parameters.keys()
    order_const = len(tokens) == 3 and tokens[1] in COMPARISON_OPS
    sum_const = (
        (len(tokens) >= 5)
        and (len(tokens) % 2 == 1)
        and (tokens[-2] in COMPARISON_OPS)
        and ("*" not in tokens)
    )
    parameter_const = (
        (len(tokens) >= 5)
        and (len(tokens) % 2 == 1)
        and (tokens[-2] in COMPARISON_OPS)
        and ("*" in tokens)
    )

    if not (order_const or sum_const or parameter_const):
        raise ValueError(
            "Parameter constraint should be of form"
            "order constraint: <parameter_name> >= <other_parameter_name>"
            "sum constraint: `<parameter_name> + <other_parameter_name> >= x`, where any number of parameters can be "
            "summed up and `x` is a float bound."
            "Acceptable comparison operators are >= and <="
            "parameter constraint: `<weight> * <parameter_name> + <other_weight> * <other_parameter_name> <= x`",
            "where any number of parameters and weights can be"
            "summed up and `x` is a float bound."
            "Only comparison operator is <="
            "<weight> can be negative",
        )

    if order_const:  # Case "x1 >= x2" => order constraint.
        left, right = tokens[0], tokens[2]
        assert left in parameter_names, f"Parameter {left} not in {parameter_names}."
        assert right in parameter_names, f"Parameter {right} not in {parameter_names}."
        return (
            OrderConstraint(
                lower_parameter=parameters[left], upper_parameter=parameters[right]
            )
            if COMPARISON_OPS[tokens[1]] is ComparisonOp.LEQ
            else OrderConstraint(
                lower_parameter=parameters[right], upper_parameter=parameters[left]
            )
        )

    try:
        bound = float(tokens[-1])
    except ValueError:
        raise ValueError(
            f"Bound for sum or parameter constraint must be a number; got {tokens[-1]}"
        )
    used_parameters = []
    used_weights = []

    if sum_const:
        for idx, token in enumerate(tokens[:-2]):
            if idx % 2 == 0:
                assert (
                    token in parameter_names
                ), f"Parameter {token} not in {parameter_names}."
                used_parameters.append(token)
            else:
                assert (
                    token == "+"
                ), f"Expected a sum constraint, found operator {token}."
        return SumConstraint(
            parameters=[parameters[p] for p in parameters if p in used_parameters],
            is_upper_bound=COMPARISON_OPS[tokens[-2]] is ComparisonOp.LEQ,
            bound=bound,
        )
    if parameter_const:
        for idx, token in enumerate(tokens[:-2]):
            if idx % 4 == 0:
                try:
                    weight = float(token)
                    used_weights.append(weight)
                except:
                    raise ValueError(
                        f"Weight for parameter constraint must be a number; got {token}"
                    )
            elif idx % 4 == 1:
                assert (
                    token == "*"
                ), f"Expected a multiplication, found operator {token}."
            elif idx % 4 == 2:
                assert (
                    token in parameter_names
                ), f"Parameter {token} not in {parameter_names}."
                used_parameters.append(token)
            else:
                assert (
                    token == "+"
                ), f"Expected a sum constraint, found operator {token}."
        return ParameterConstraint(
            constraint_dict=dict(zip(used_parameters, used_weights)), bound=bound
        )

def outcome_constraint_from_str(representation: str) -> OutcomeConstraint:
    """Parse string representation of an outcome constraint."""
    tokens = representation.split()
    assert len(tokens) == 3 and tokens[1] in COMPARISON_OPS, (
        "Outcome constraint should be of form `metric_name >= x`, where x is a "
        "float bound and comparison operator is >= or <=."
    )
    op = COMPARISON_OPS[tokens[1]]
    rel = False
    try:
        bound_repr = tokens[2]
        if bound_repr[-1] == "%":
            rel = True
            bound_repr = bound_repr[:-1]
        bound = float(bound_repr)
    except ValueError:
        raise ValueError("Outcome constraint bound should be a float.")
    return OutcomeConstraint(Metric(name=tokens[0]), op=op, bound=bound, relative=rel)


def make_experiment(
    parameters: List[TParameterRepresentation],
    name: Optional[str] = None,
    objective_name: Optional[str] = None,
    minimize: bool = False,
    parameter_constraints: Optional[List[str]] = None,
    outcome_constraints: Optional[List[str]] = None,
    status_quo: Optional[TParameterization] = None,
    experiment_type: Optional[str] = None,
) -> Experiment:
    """Instantiation wrapper that allows for creation of SimpleExperiment without
    importing or instantiating any Ax classes."""

    exp_parameters: List[Parameter] = [parameter_from_json(p) for p in parameters]
    status_quo_arm = None if status_quo is None else Arm(parameters=status_quo)
    parameter_map = {p.name: p for p in exp_parameters}
    ocs = [outcome_constraint_from_str(c) for c in (outcome_constraints or [])]
    if status_quo_arm is None and any(oc.relative for oc in ocs):
        raise ValueError("Must set status_quo to have relative outcome constraints.")
    return Experiment(
        name=name,
        search_space=SearchSpace(
            parameters=exp_parameters,
            parameter_constraints=None
            if parameter_constraints is None
            else [constraint_from_str(c, parameter_map) for c in parameter_constraints],
        ),
        optimization_config=OptimizationConfig(
            objective=Objective(
                metric=Metric(name=objective_name or DEFAULT_OBJECTIVE_NAME),
                minimize=minimize,
            ),
            outcome_constraints=ocs,
        ),
        status_quo=status_quo_arm,
        experiment_type=experiment_type,
    )


def raw_data_to_evaluation(
    raw_data: TEvaluationOutcome,
    objective_name: str,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> TEvaluationOutcome:
    """Format the trial evaluation data to a standard `TTrialEvaluation`
    (mapping from metric names to a tuple of mean and SEM) representation, or
    to a TFidelityTrialEvaluation.

    Note: this function expects raw_data to be data for a `Trial`, not a
    `BatchedTrial`.
    """
    if isinstance(raw_data, dict):
        if any(isinstance(x, dict) for x in raw_data.values()):  # pragma: no cover
            raise ValueError("Raw data is expected to be just for one arm.")
        return raw_data
    elif isinstance(raw_data, list):
        return raw_data
    elif isinstance(raw_data, tuple):
        return {objective_name: raw_data}
    elif isinstance(raw_data, (float, int)):
        return {objective_name: (raw_data, None)}  # pyre-fixme[7]
    elif isinstance(raw_data, (np.float32, np.float64, np.int32, np.int64)):
        return {objective_name: (numpy_type_to_python_type(raw_data), None)}
    else:
        raise ValueError(
            "Raw data has an invalid type. The data must either be in the form "
            "of a dictionary of metric names to mean, sem tuples, "
            "or a single mean, sem tuple, or a single mean."
        )


def data_from_evaluations(
    evaluations: Dict[str, TEvaluationOutcome],
    trial_index: int,
    sample_sizes: Dict[str, int],
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> Data:
    """Transforms evaluations into Ax Data.

    Each evaluation is either a trial evaluation: {metric_name -> (mean, SEM)}
    or a fidelity trial evaluation for multi-fidelity optimizations:
    [(fidelities, {metric_name -> (mean, SEM)})].

    Args:
        evalutions: Mapping from arm name to evaluation.
        trial_index: Index of the trial, for which the evaluations are.
        sample_sizes: Number of samples collected for each arm, may be empty
            if unavailable.
        start_time: Optional start time of run of the trial that produced this
            data, in milliseconds.
        end_time: Optional end time of run of the trial that produced this
            data, in milliseconds.
    """
    if all(isinstance(evaluations[x], dict) for x in evaluations.keys()):
        # All evaluations are no-fidelity evaluations.
        data = Data.from_evaluations(
            evaluations=cast(Dict[str, TTrialEvaluation], evaluations),
            trial_index=trial_index,
            sample_sizes=sample_sizes,
            start_time=start_time,
            end_time=end_time,
        )
    elif all(isinstance(evaluations[x], list) for x in evaluations.keys()):
        # All evaluations are with-fidelity evaluations.
        data = Data.from_fidelity_evaluations(
            evaluations=cast(Dict[str, TFidelityTrialEvaluation], evaluations),
            trial_index=trial_index,
            sample_sizes=sample_sizes,
            start_time=start_time,
            end_time=end_time,
        )
    else:
        raise ValueError(  # pragma: no cover
            "Evaluations included a mixture of no-fidelity and with-fidelity "
            "evaluations, which is not currently supported."
        )
    return data
