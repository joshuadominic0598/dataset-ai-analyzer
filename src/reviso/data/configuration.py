"""Validate dataset setup independently of storage."""

import math

from ..exploration import DATASET_TYPES, DATE_FORMATS, ROLES

def validate_config(config, incoming, columns):
    for key in ("identity", "important", "semantic"):
        value = incoming.get(key, config[key])
        if not isinstance(value, list) or any(
            not isinstance(c, str) or c not in columns for c in value
        ):
            raise ValueError(f"Choose existing columns for {key}.")
        config[key] = list(dict.fromkeys(value))
    for key in ("cloud_allowed", "setup_complete"):
        if key in incoming:
            if not isinstance(incoming[key], bool):
                raise ValueError(f"{key} must be true or false.")
            config[key] = incoming[key]
    for key in ("growth_threshold", "missing_threshold"):
        if key in incoming:
            number = float(incoming[key])
            if not math.isfinite(number) or not 0 < number <= 1000:
                raise ValueError("Thresholds must be between 0 and 1,000.")
            config[key] = number
    config["context"] = str(incoming.get("context", config["context"]))[:3000]
    ep = incoming.get("embedding_provider", config["embedding_provider"])
    if ep not in ("", "ollama", "openai", "gemini"):
        raise ValueError("Text embeddings need an Ollama, OpenAI, or Gemini connection.")
    config["embedding_provider"] = ep
    config["embedding_model"] = str(incoming.get("embedding_model", config["embedding_model"]))[
        :120
    ]
    decisions = incoming.get("rename_decisions", config.get("rename_decisions", []))
    if not isinstance(decisions, list) or len(decisions) > 100:
        raise ValueError("Use up to 100 field-link decisions.")
    seen = set()
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or decision.get("old") not in columns
            or decision.get("new") not in columns
            or decision["old"] == decision["new"]
            or decision.get("decision") not in ("confirm", "reject")
            or (decision["old"], decision["new"]) in seen
        ):
            raise ValueError(
                "Choose existing, different columns and confirm or reject each link once."
            )
        seen.add((decision["old"], decision["new"]))
    config["rename_decisions"] = decisions
    dataset_type = incoming.get("dataset_type", config["dataset_type"])
    if dataset_type not in DATASET_TYPES:
        raise ValueError("Choose a supported dataset type.")
    config["dataset_type"] = dataset_type
    for key, maximum in (("analysis_goal", 2000), ("row_meaning", 1000)):
        value = incoming.get(key, config[key])
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f"{key} must be text of at most {maximum} characters.")
        config[key] = value
    for key in ("date_column", "target_column"):
        value = incoming.get(key, config[key])
        if not isinstance(value, str) or (value and value not in columns):
            raise ValueError(f"Choose an existing field for {key}.")
        config[key] = value
    for key, choices in (("date_format", DATE_FORMATS), ("time_grain", {"day", "month"})):
        value = incoming.get(key, config[key])
        if value not in choices:
            raise ValueError(f"Choose a supported {key}.")
        config[key] = value
    roles = incoming.get("column_roles", config["column_roles"])
    orders = incoming.get("category_orders", config["category_orders"])
    if not isinstance(roles, dict) or any(
        c not in columns or role not in ROLES for c, role in roles.items()
    ):
        raise ValueError("Field meanings must use existing fields and supported roles.")
    if not isinstance(orders, dict):
        raise ValueError("Category orders must be a field-to-labels mapping.")
    for column, labels in orders.items():
        if (
            column not in columns
            or not isinstance(labels, list)
            or not 2 <= len(labels) <= 100
            or any(not isinstance(label, str) or not label or len(label) > 200 for label in labels)
            or len(set(labels)) != len(labels)
        ):
            raise ValueError(
                "An ordinal order needs 2–100 different text labels for an existing field."
            )
    config["column_roles"], config["category_orders"] = roles, orders
    return config
