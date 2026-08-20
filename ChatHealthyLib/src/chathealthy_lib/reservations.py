"""Reservations: one holder at a time.

A service that must not run twice takes a reservation before it does
anything and gives it back when it is done. The collection holds one record
or none: a record means the service is running.

    look        a record is there    -> abend, the service is running
                no record            -> write one
    confirm     exactly one record   -> proceed
                more than one        -> two invocations wrote at the same
                                        moment; withdraw and abend

The confirm step is what makes the race safe. Looking and then writing has a
window between them, and two invocations can both look, both see nothing,
and both write. Counting afterwards catches that: the count is taken after
both writes have landed, so neither can read one and miss the other. Both
withdraw and both abend, which is the safe outcome -- no migration runs when
it is unclear which invocation owns the service, and the operator asks again.

This lives in the library rather than in the service that reserves, because
admitting a job and performing one are different concerns. The service asks
whether it may run; what it does once it may is no business of this module.
"""
from __future__ import annotations

import datetime

from .exceptions import ChatHealthyException
from .mongo_utilities import ChatHealthyMongoUtilities

RESERVATIONS_DATABASE = "pipelineAdmin"
RESERVATIONS_COLLECTION = "DataMigrationServiceMutex"


def _collection(identity: str, cluster: str):
    return ChatHealthyMongoUtilities().getConnection(
        identity, cluster)[RESERVATIONS_DATABASE][RESERVATIONS_COLLECTION]


def _raise_held(reservation_type: str, holder: str, about: str,
                held: dict) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="reservation_held",
        component="reservations",
        message=(f"{reservation_type!r} is reserved by {held.get('holder')!r} "
                 f"since {held.get('reserved_at')} for {held.get('about')!r}; "
                 f"{holder!r} was refused"),
        reservation_type=reservation_type,
        about=about)


def _raise_contended(reservation_type: str, holder: str, about: str,
                     found: int) -> None:
    raise ChatHealthyException(
        mode="reservation_contended",
        component="reservations",
        message=(f"{found} reservations for {reservation_type!r} exist at "
                 f"once: another invocation wrote one at the same moment. "
                 f"{holder!r} withdrew its own and refused; no holder is "
                 f"assumed"),
        reservation_type=reservation_type,
        about=about)


def reserve(identity: str, cluster: str, reservation_type: str,
            holder: str, about: str = "") -> None:
    """Take the reservation, or raise.

    There is one record or none. The record's id is the type, so there is
    nothing to hand back and forth and nothing to look up.
    """
    collection = _collection(identity, cluster)

    # Any reservation at all, not one of this type. The collection holds one
    # record or none, and a record means something is running.
    held = collection.find_one({})
    if held is not None:
        _raise_held(reservation_type, holder, about, held)

    now = datetime.datetime.now(datetime.timezone.utc)
    collection.insert_one({
        "_id": reservation_type,
        "reservation_type": reservation_type,
        "holder": holder,
        "about": about,
        "reserved_at": now,
        "day": now.date().isoformat(),
    })

    # The whole collection, not this record. Counting by id proves nothing --
    # an id is unique, so that count is one by definition. More than one
    # reservation of any kind means the service's state is not what this
    # invocation believes, and it abends rather than acting on it.
    found = collection.count_documents({})
    if found != 1:
        collection.delete_one({"_id": reservation_type})
        _raise_contended(reservation_type, holder, about, found)


def release(identity: str, cluster: str, reservation_type: str) -> None:
    """Delete the reservation, so the next holder can take it."""
    _collection(identity, cluster).delete_one({"_id": reservation_type})
