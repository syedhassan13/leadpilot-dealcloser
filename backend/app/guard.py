"""Guard agent (Lab 03 safety): blocks prompt injection, data-snooping and off-topic requests IN CODE,
before any model call. Deterministic, free and instant. The closer's prompt is the second line of defence.
"""
import re

# CUSTOM: our injection / data-leak policy
INJECTION = re.compile(
    r"ignore (all |any |the |your )?(previous|prior|above|earlier|system)?\s*(instructions|rules|prompts?)"
    r"|disregard (all |your |the )?(previous |prior )?(instructions|rules)"
    r"|forget (all |your |the )?(previous |prior )?(instructions|rules)"
    r"|(reveal|show|print|repeat|tell me|give me|what is|what's) (me )?(your |the )?(system|hidden|initial|original) (prompt|instructions|message)"
    r"|\bsystem prompt\b|\bdeveloper mode\b|\bjailbreak|(?-i:\bDAN\b)|you are now (a|an|in)\b|act as (a|an|my) (?!buyer|customer)"
    r"|pretend (to be|you are)|new instructions:|override (your|the) (rules|instructions)"
    r"|\b(admin|api) (token|key|password)\b|\banthropic_api_key\b|\.env\b"
    r"|(other|another|all|every) (users?|customers?|clients?|leads?|buyers?)('s?)? (data|details|info|information|bookings?|numbers?|phones?|leads?|conversations?|chats?)"
    r"|(show|list|give|export|dump) (me )?(all|every) (the )?(users|customers|clients|leads|bookings|phone numbers)"
    r"|(data|details|bookings?|conversations?|chats?|phone number|budget) of (user|customer|client) \S+"
    r"|\b(show|give|tell|share|send|list|reveal|access|see|check|what is|what are|what's) (me )?(?!my\b|our\b)[a-z]+'s "
    r"(bookings?|visits?|phone|number|contact|data|details|budget|address|chats?|conversations?|leads?|profile)\b"
    r"|\b(system|admin|developer|assistant) (note|message|override|instruction|command)s?\b|\[(system|admin)\]"
    r"|without (asking|confirmation|confirming|permission)|(customer|user) (has )?already confirmed"
    r"|\b(book_visit|cancel_booking|save_lead_details|search_inventory|check_availability|search_faq|my_bookings|get_listing)\b"
    r"|\buser_id\b|\bthread_id\b|\bselect\s+\*\s*from\b|\bdrop\s+table\b|\bdelete\s+from\b|\binsert\s+into\b|\bunion\s+select\b",
    re.I)

# CUSTOM: clearly off-topic asks. Anything subtler is handled by the closer's prompt.
OFF_TOPIC = re.compile(
    r"\b(write|make|create|generate|compose) (me )?(a |an |some )?(python |java |c\+\+ |js )?(code|program|script|function|poem|essay|song|lyrics|story|joke|rap|assignment|homework|cv|resume|letter)\b"
    r"|\b(python|javascript|java|c\+\+|html|sql|leetcode)\b.*\b(code|program|function|bug|error|solve)\b"
    r"|\bcapital of\b|\bwho (won|is the (president|prime minister))\b|\b(cricket|football) (score|match)\b"
    r"|\brecipe\b|\bhoroscope\b|\bsolve (this|the|my) (equation|math|problem)\b"
    r"|\b(bitcoin|crypto|stock) (price|tips?)\b|\bmeaning of life\b|\bweather (today|tomorrow|in)\b",
    re.I)

INJECTION_REPLY = ("I can't do that. I only help with buying property or cars from our partner businesses, and I "
                   "can never share other customers' information or my internal instructions. "
                   "Are you looking for a property or a car today?")
OFF_TOPIC_REPLY = ("Sorry, I can only help with buying property or cars: listings, prices, financing, and booking "
                   "site visits or test drives. What are you looking for?")
EMPTY_REPLY = "Hi! I'm LeadPilot. Are you looking for a property or a car?"


def check(text: str) -> str | None:
    """Return a refusal message if the text must be blocked, else None."""
    t = (text or "").strip()
    if not t:
        return EMPTY_REPLY
    if INJECTION.search(t):
        return INJECTION_REPLY
    if OFF_TOPIC.search(t):
        return OFF_TOPIC_REPLY
    return None
