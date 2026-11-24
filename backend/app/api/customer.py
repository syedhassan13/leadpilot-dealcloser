"""Customer endpoints. Every route needs the signed X-User-Token issued with the customer id."""
from fastapi import APIRouter, Header

from app.security import check_customer, log_event
from app.services import bookings_of, erase_customer, profile_of, thread_ids, thread_messages

router = APIRouter(prefix="/customer/{user_id}")


@router.get("/threads/{thread_id}")
def customer_thread(user_id: str, thread_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    return {"messages": thread_messages(user_id, thread_id)}


@router.get("/threads")
def customer_threads(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    return {"threads": thread_ids(user_id)}


@router.get("/bookings")
def customer_bookings(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    return bookings_of(user_id)


@router.get("/profile")
def customer_profile(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    return {"profile": profile_of(user_id)}


@router.delete("")
def customer_delete(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    erase_customer(user_id)
    log_event("customer_deleted_own_data", "")
    return {"deleted": True}
