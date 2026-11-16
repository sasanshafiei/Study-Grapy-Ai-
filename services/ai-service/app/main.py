"""Mock AI service.

CE-1 owns this file *for now*: it returns canned fixtures so the backend and
the frontend can be built before the AI pair (AI-1, AI-2) ship the real thing.
The AI students replace these handlers one endpoint at a time, keeping the
same request/response shapes defined in contracts/ai-service.openapi.yaml.

Everything here is fake. The point is only that the shapes are real.
"""

from fastapi import FastAPI

app = FastAPI(title="StudyGraph AI Service (mock)", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-service", "mock": True}


@app.post("/ai/v1/index/documents")
def index_documents(payload: dict):
    return {"document_id": payload.get("document_id", "d_mock"), "chunk_count": 12,
            "topic_tags": ["t_stoich", "t_equilibrium"]}


@app.post("/ai/v1/topics/extract")
def topics_extract(payload: dict):
    return {
        "course_id": payload.get("course_id", "c_mock"),
        "topics": [
            {"id": "t_stoich", "name": "Stoichiometry",
             "queries": ["limiting reactant worked example", "mole ratio practice"]},
            {"id": "t_equilibrium", "name": "Equilibrium",
             "queries": ["Le Chatelier principle examples", "equilibrium constant Kc"]},
        ],
    }


@app.post("/ai/v1/chat/answer")
def chat_answer(payload: dict):
    return {
        "answer": "The limiting reactant is the one that runs out first [1]. "
                  "Compare mole ratios to find it [2].",
        "citations": [
            {"marker": 1, "chunk_id": "c_81f", "origin": "class",
             "title": "Lecture_04_Stoichiometry.pdf", "locator": {"page": 6},
             "url": None, "snippet": "The limiting reactant limits the amount of product..."},
            {"marker": 2, "chunk_id": "c_a02", "origin": "external",
             "title": "Chemistry 2e, 4.4 Reaction Yields", "locator": {"heading": "4.4"},
             "url": "https://openstax.org/books/chemistry-2e/pages/4-4",
             "snippet": "A reaction yield is..."},
        ],
        "grounded": True,
        "confidence": 0.82,
        "used_web": False,
        "topic_ids": ["t_stoich"],
    }


@app.post("/ai/v1/mastery/update")
def mastery_update(payload: dict):
    return {
        "user_id": payload.get("user_id", "u_mock"),
        "course_id": payload.get("course_id", "c_mock"),
        "mastery": [
            {"topic_id": "t_stoich", "mastery": 0.74, "trend": "up", "next_review_at": "2026-11-20"},
            {"topic_id": "t_equilibrium", "mastery": 0.38, "trend": "down", "next_review_at": "2026-11-18"},
        ],
    }


@app.post("/ai/v1/recommendations")
def recommendations(payload: dict):
    return {
        "items": [
            {"type": "external_resource", "topic_id": "t_equilibrium",
             "title": "Le Chatelier's principle, worked examples",
             "target": {"document_id": "d_44c", "url": "https://example.org/le-chatelier"},
             "priority": 0.91,
             "reason": {"text": "3 of your last 5 equilibrium answers were wrong",
                        "mastery": 0.38, "evidence_attempt_ids": ["a_1", "a_2", "a_3"]}},
        ],
    }
