from dotenv import load_dotenv
from openai import OpenAI
from anthropic import Anthropic
import copy
import json
import os
import requests
from datetime import datetime
from pypdf import PdfReader
import gradio as gr
from ChatHealthyMongoUtilities import ChatHealthyMongoUtilities


load_dotenv(override=True)

# MongoDB - lazy connection (connects on first use; app starts even if Mongo is unreachable)
_mongo_conn_str = os.getenv("MONGO_connectionString") or ""
DBManager = ChatHealthyMongoUtilities(
    _mongo_conn_str,
    connect_timeout_ms=10000,
    server_selection_timeout_ms=15000,
    max_retries=2,
) if _mongo_conn_str else None


def _get_db():
    """Returns MongoDB database or None if connection fails or missing. Logs and continues on failure."""
    if DBManager is None:
        return None
    try:
        return DBManager.getConnection()
    except Exception as e:
        print(f"MongoDB unavailable (chat will work; recordings skipped): {e}", flush=True)
        return None


# Pushover
pushover_user = os.getenv("PUSHOVER_USER")
pushover_token = os.getenv("PUSHOVER_TOKEN")
pushover_url = "https://api.pushover.net/1/messages.json"


def push(message):
    print(f"Push: {message}")
    payload = {"user": pushover_user, "token": pushover_token, "message": message}
    requests.post(pushover_url, data=payload)


def commitSignificantActivity(payload=None, **kwargs):
    """Accept a JSON payload and insert into DB. Payload: {database, collection, record}."""
    from datetime import datetime
    db = _get_db()
    if db is None:
        return {"recorded": "ok", "note": "MongoDB unavailable; record not persisted"}
    payload = payload or kwargs
    if isinstance(payload, str):
        payload = json.loads(payload)
    database = payload["database"]
    collection = payload["collection"]
    record = dict(payload["record"])
    record["record_number"] = db[database][collection].count_documents({}) + 1
    record["datetime"] = datetime.now().isoformat()
    db[database][collection].insert_one(record)
    return {"recorded": "ok"}


def _format_chat_history(messages):
    """Extract user/assistant content from messages for storage."""
    out = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        c = m.get("content")
        text = str(c)[:500] if c else ""
        out.append({"role": m["role"], "content": text})
    return out


def record_user_details(email="", name="Name not provided", notes="not provided", message="",
                        chat_history=None, consent_verbatim=False, consent_summary=None):
    if not email or not str(email).strip():
        return {"recorded": "ok", "note": "Email required but not provided"}
    db = _get_db()
    if db is None:
        push(f"Recording interest from {name} with email {email} (DB unavailable)")
        return {"recorded": "ok", "note": "MongoDB unavailable; contact logged via push only"}
    reason = message or notes
    lead_coll = db["AboutUs"]["lead"]
    for doc in lead_coll.find():
        if email in str(doc.get("email", "")):
            return {"recorded": "ok"}
    push(f"Recording interest from {name} with email {email}: {reason}")
    record = {
        "email": email,
        "name": name,
        "notes": notes,
        "reason_for_contact": reason,
        "consent_verbatim": consent_verbatim,
        "consent_summary": consent_summary,
        "datetime": datetime.now().isoformat(),
    }
    if consent_verbatim:
        record["chat_history"] = chat_history or []
    elif consent_summary:
        history_copy = copy.deepcopy(chat_history) if chat_history else []
        deIdentify(history_copy)
        record["chat_history"] = history_copy
    payload = {"database": "AboutUs", "collection": "lead", "record": record}
    commitSignificantActivity(payload)
    return {"recorded": "ok"}


def deIdentify(argChat_history):
    """
    Takes the list used to create the history array in the output object, and deidentifies each member.
    Deidentification is sufficient for HIPAA Research Data. Uses Anthropic model claude-haiku-4-5-20251001.
    Batches the entire conversation in a single API call. Mutates argChat_history in place.
    """
    if not argChat_history:
        return
    client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
    model = "claude-haiku-4-5-20251001"
    chat_json = json.dumps([{"role": m.get("role", ""), "content": m.get("content") or ""} for m in argChat_history], indent=2)
    deidentify_prompt = """Deidentify the following chat conversation so it meets HIPAA Safe Harbor requirements for research data.
Remove or replace: names, geographic identifiers (except state), dates (except year), phone/fax, email, SSN,
medical record numbers, account numbers, license numbers, vehicle identifiers, device identifiers, URLs, IP addresses,
and any other identifiers that could be used to identify an individual.
Preserve the semantic meaning of each message for research purposes.

Return ONLY a valid JSON array of strings, one string per message in the same order. Each string is the deidentified content for that message.
Example: ["deidentified msg 1", "deidentified msg 2"]

Chat conversation:
"""
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": f"{deidentify_prompt}\n{chat_json}"}],
    )
    result_text = response.content[0].text.strip()
    if result_text.startswith("```"):
        result_text = result_text.split("```")[1]
        if result_text.startswith("json"):
            result_text = result_text[4:]
        result_text = result_text.strip()
    deidentified_contents = json.loads(result_text)
    for i, content in enumerate(deidentified_contents):
        if i < len(argChat_history):
            argChat_history[i]["content"] = content


def record_unknown_question(question, chat_history=None):
    if chat_history is not None:
        deIdentify(chat_history)
    push(f"Recording a user question I could not answer: {question}")
    payload = {
        "database": "AboutUs", "collection": "AboutSkip",
        "record": {"question": question, "chat_history": chat_history or []}
    }
    commitSignificantActivity(payload)
    return {"recorded": "ok"}


record_user_details_json = {
    "name": "record_user_details",
    "description": (
        "Record a user's contact details after obtaining email. "
        "Before calling this tool you MUST complete the two-tier consent flow: "
        "First ask: 'May we save a verbatim transcript of this conversation with your contact details?' "
        "If they decline, ask: 'May we save a de-identified summary of this conversation instead?' "
        "Pass both answers as consent_verbatim and consent_summary. "
        "Try to get their name and reason for contact, but do not insist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "The email address of this user"},
            "name": {"type": "string", "description": "The user's name, if they provided it"},
            "notes": {"type": "string", "description": "Summarize the conversation in less than 40 words"},
            "message": {"type": "string", "description": "Why they are contacting us"},
            "consent_verbatim": {"type": "boolean", "description": "True if user agreed to save verbatim transcript"},
            "consent_summary": {"type": "boolean", "description": "True if user agreed to save de-identified summary. Only set when consent_verbatim is false. Null if verbatim was accepted."}
        },
        "required": ["email", "notes", "consent_verbatim"],
        "additionalProperties": False
    }
}

record_unknown_question_json = {
    "name": "record_unknown_question",
    "description": (
        "Call this tool BEFORE composing your response whenever ANY of the following is true: "
        "(1) The answer is not explicitly stated in the provided Summary, LinkedIn, or Anthropic documents. "
        "(2) You would use any hedging word such as 'I think', 'probably', 'might', 'I believe', 'I'm not sure', "
        "'it seems', 'I'd imagine', 'I'd guess', or any similar qualifier. "
        "(3) The question involves any medical, clinical, health, or treatment topic — always record and decline these, "
        "no exceptions and no caveats. "
        "(4) You are inferring or extrapolating rather than directly quoting the provided documents. "
        "Do NOT answer first and record second. The correct order is: call this tool, then tell the user you don't have that information."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The exact question or topic that could not be answered with certainty from the provided documents"}
        },
        "required": ["question"],
        "additionalProperties": False
    }
}

commitSignificantActivity_json = {
    "name": "commitSignificantActivity",
    "description": "Record any custom activity to the database. Supply database, collection, and record (any JSON structure). Use for custom records beyond contacts and unknown questions.",
    "parameters": {
        "type": "object",
        "properties": {
            "database": {"type": "string", "description": "Database name"},
            "collection": {"type": "string", "description": "Collection name"},
            "record": {"type": "object", "description": "The document to insert - any JSON object. record_number and datetime are added automatically."}
        },
        "required": ["database", "collection", "record"]
    }
}

tools = [
    {"type": "function", "function": record_user_details_json},
    {"type": "function", "function": record_unknown_question_json},
    {"type": "function", "function": commitSignificantActivity_json}
]


# Path to me/ directory relative to this script (works for local runs and deployment)
_ME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "me")


class Me:

    def __init__(self):
        self.openai = OpenAI()
        self.name = "Skip Snow"
        self.website = "ChatHealthy.AI"
        reader = PdfReader(os.path.join(_ME_DIR, "SkipSnowLinkedInProfile.pdf"))
        self.linkedin = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                self.linkedin += text
        reader_anthropic = PdfReader(os.path.join(_ME_DIR, "BuildingAnthropicAConversationWithItsCo-foundersYouTube.pdf"))
        self.anthropic_discussion = ""
        for page in reader_anthropic.pages:
            text = page.extract_text()
            if text:
                self.anthropic_discussion += text
        with open(os.path.join(_ME_DIR, "summary.txt"), "r", encoding="utf-8") as f:
            self.summary = f.read()

    def handle_tool_calls(self, tool_calls, messages=None):
        chat_history = _format_chat_history(messages) if messages else []
        results = []
        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            if tool_name in ("record_user_details", "record_unknown_question"):
                arguments["chat_history"] = chat_history
            print(f"Tool called: {tool_name}", flush=True)
            tool = globals().get(tool_name)
            result = tool(**arguments) if tool else {}
            results.append({"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id})
        return results

    def system_prompt(self):
        return (
            f"You are acting as {self.name}. You are answering questions on {self.website}'s website, "
            f"particularly questions related to {self.name}'s career, background, skills and experience and plans the future of this web site. "
            f"Your responsibility is to represent {self.name} and {self.website} for interactions on the website as faithfully as possible. "
            f"You are given a summary of {self.name}'s background and LinkedIn profile which you can use to answer questions. "
            f"Be professional and engaging, as if talking to a potential client or future employer who came across the website. "
            f"\n\n## STRICT ANSWER RULES — NO EXCEPTIONS\n"
            f"RULE 1 — SOURCE RESTRICTION: You may ONLY answer from facts explicitly stated in the Summary, LinkedIn, and Anthropic documents provided below. "
            f"You must NEVER use your general training knowledge to answer. If it is not in the documents, you do not know it.\n"
            f"RULE 2 — NO HEDGING: You are PROHIBITED from using any hedging language: 'I think', 'probably', 'might', "
            f"'I believe', 'I'm not sure', 'it seems', 'I'd imagine', 'I'd guess', or similar. "
            f"If you would reach for any of these words, that is your signal to call record_unknown_question instead of answering.\n"
            f"RULE 3 — MEDICAL/HEALTH TOPICS: ANY question touching on medical advice, clinical information, treatments, diagnoses, "
            f"or health recommendations must be declined without exception. Call record_unknown_question first, "
            f"then tell the user this is not something you can advise on and they should consult a qualified professional.\n"
            f"RULE 4 — TOOL CALL ORDER: Always call record_unknown_question BEFORE composing your response. Never answer first and record second.\n"
            f"RULE 5 — EACH QUESTION SEPARATELY: If a user asks multiple questions in one message and some are unknown, "
            f"record each unknown question with a separate tool call.\n"
            f"RULE 6 — FOLLOW-UP OFFER: When you receive a system FOLLOW-UP CHECK reminder, first review the conversation "
            f"for any sign of annoyance or reluctance at being asked about follow-up — such as 'stop asking', 'I already said no', "
            f"'leave me alone', 'not interested', or any impatient or irritated tone in response to a previous follow-up offer. "
            f"If any such signal exists, do NOT ask again — ignore the reminder entirely for the rest of the conversation. "
            f"Otherwise, assess whether the user has shown genuine interest in a specific topic — such as career inquiry, "
            f"investment, partnership, product interest, or healthcare navigation — AND you do not yet have their contact details. "
            f"If both are true, ask: 'Would you like someone from the ChatHealthy.AI team to follow up with you personally?' "
            f"If they say yes, proceed to collect their email and complete the consent flow. "
            f"If context is not yet sufficient (e.g. the user has only exchanged one or two brief messages), do not ask yet.\n"
            f"\n## EXAMPLES\n"
            f"User: What is your favorite poem?\n"
            f"WRONG: 'While I appreciate poetry, I don't have a specific favorite.' [no tool call — this is a violation]\n"
            f"RIGHT: [call record_unknown_question] then say: 'I don't have that information — I've noted your question and will follow up.'\n"
            f"User: Should I take ibuprofen for my back pain?\n"
            f"WRONG: 'While I'm not a doctor, ibuprofen can help with inflammation...' [medical advice — this is a violation]\n"
            f"RIGHT: [call record_unknown_question] then say: 'I can't advise on medical questions. Please consult a qualified healthcare professional.'\n\n"
            f"If the user is engaging in discussion, try to steer them towards getting in touch via email, phone or linkedin; ask for their email, or other method of communication. No authentication needed. "
            f"When you have their email, complete the two-tier consent flow before calling record_user_details:\n"
            f"  Step 1 — Ask: 'May we save a verbatim transcript of this conversation with your contact details?'\n"
            f"  Step 2 — If they decline Step 1, ask: 'May we save a de-identified summary of this conversation instead?'\n"
            f"  Then call record_user_details with consent_verbatim and (if asked) consent_summary reflecting their answers.\n"
            f"Call record_user_details only ONCE per contact. If you have already recorded this user's email in this conversation, do not call it again. "
            f"If the user gives an email and you don't know their name, capture their name too.\n\n"
            f"## Summary:\n{self.summary}\n\n## LinkedIn Profile:\n{self.linkedin}\n\n"
            f"## AnthropicOnSafety:\n{self.anthropic_discussion}\n\n"
            f"With this context, please chat with the user, always staying in character as {self.name}."
        )

    def chat(self, message, history):
        messages = [{"role": "system", "content": self.system_prompt()}] + history
        user_msg_count = sum(1 for m in history if m.get("role") == "user")
        if user_msg_count > 0 and user_msg_count % 5 == 0:
            messages.append({
                "role": "system",
                "content": (
                    "FOLLOW-UP CHECK: Review the conversation. If the user has shown genuine interest "
                    "in a specific topic and you do not yet have their contact details, ask now: "
                    "'Would you like someone from the ChatHealthy.AI team to follow up with you personally?'"
                )
            })
        messages.append({"role": "user", "content": message})
        done = False
        while not done:
            response = self.openai.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=tools)
            if response.choices[0].finish_reason == "tool_calls":
                msg = response.choices[0].message
                tool_calls = msg.tool_calls
                results = self.handle_tool_calls(tool_calls, messages)
                messages.append(msg)
                messages.extend(results)
            else:
                done = True
        return response.choices[0].message.content


if __name__ == "__main__":
    me = Me()
    gr.ChatInterface(
        me.chat,
        type="messages",
        title="Chat Healthy: About Us",
        css="footer { display: none !important; }",
    ).launch(share=True, show_api=False)
