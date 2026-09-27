from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.RISK_APPROVED, OrderStatus.REJECTED},
    OrderStatus.RISK_APPROVED: {OrderStatus.SUBMITTING, OrderStatus.REJECTED},
    OrderStatus.SUBMITTING: {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.UNKNOWN},
    OrderStatus.ACCEPTED: {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN},
    OrderStatus.OPEN: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED, OrderStatus.UNKNOWN},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED, OrderStatus.UNKNOWN},
    OrderStatus.CANCEL_PENDING: {OrderStatus.CANCELLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.UNKNOWN},
    OrderStatus.UNKNOWN: {OrderStatus.ACCEPTED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED},
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
}


def transition(current: OrderStatus, target: OrderStatus) -> OrderStatus:
    if target not in _ALLOWED[current]:
        raise ValueError(f"invalid order transition: {current} -> {target}")
    return target
