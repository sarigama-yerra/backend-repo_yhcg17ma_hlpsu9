import os
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from database import db, create_document, get_documents
from bson import ObjectId
import re
import requests

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------
# Utility
# ----------------------------

def oid_str(oid):
    try:
        return str(oid)
    except Exception:
        return oid


def to_serializable(doc):
    if not doc:
        return doc
    d = {**doc}
    if "_id" in d:
        d["id"] = oid_str(d.pop("_id"))
    return d


# ----------------------------
# Root & Health
# ----------------------------
@app.get("/")
def read_root():
    return {"message": "Backend pronto 🚀"}


@app.get("/api/hello")
def hello():
    return {"message": "Ciao dal backend API!"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# ----------------------------
# Schemas endpoint (for viewers)
# ----------------------------
@app.get("/schema")
def get_schemas():
    from schemas import User, Product, Stop, BusLine
    return {
        "user": User.model_json_schema(),
        "product": Product.model_json_schema(),
        "stop": Stop.model_json_schema(),
        "busline": BusLine.model_json_schema(),
    }


# ----------------------------
# Bus domain
# ----------------------------
class StopModel(BaseModel):
    name: str
    travel_minutes_from_prev: int


class BusLineModel(BaseModel):
    name: str
    language: Optional[str] = "it"
    stops: List[StopModel] = []


BUS_COLLECTION = "busline"


@app.get("/api/bus/lines")
def list_lines():
    docs = get_documents(BUS_COLLECTION)
    return [to_serializable(d) for d in docs]


@app.post("/api/bus/lines")
def create_line(line: BusLineModel):
    # Validate at least one stop
    if not line.stops or len(line.stops) == 0:
        raise HTTPException(status_code=400, detail="La linea deve avere almeno una fermata")
    inserted_id = create_document(BUS_COLLECTION, line.model_dump())
    doc = db[BUS_COLLECTION].find_one({"_id": ObjectId(inserted_id)})
    return to_serializable(doc)


@app.get("/api/bus/lines/{line_id}")
def get_line(line_id: str):
    try:
        doc = db[BUS_COLLECTION].find_one({"_id": ObjectId(line_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="ID non valido")
    if not doc:
        raise HTTPException(status_code=404, detail="Linea non trovata")
    return to_serializable(doc)


# Heuristic OCR parsing using OCR.Space API if available
# Provide OCR_SPACE_API_KEY in env to enable

def parse_text_to_stops(text: str) -> List[dict]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    stops: List[dict] = []
    last_time: Optional[int] = None

    time_line_pattern = re.compile(r"^(?P<name>.+?)\s+(-\s*)?(?P<min>\d{1,3})\s*(min|m|minutes?)?$", re.I)
    hhmm_pattern = re.compile(r"(\d{1,2}):(\d{2})")

    for l in lines:
        m = time_line_pattern.match(l)
        if m:
            name = m.group("name").strip("-• ")
            minutes = int(m.group("min"))
            stops.append({"name": name, "travel_minutes_from_prev": minutes})
            last_time = None
            continue
        # Try to parse HH:MM sequences on a line
        times = [(int(h), int(m)) for h, m in hhmm_pattern.findall(l)]
        if len(times) >= 2:
            # Convert to minutes from start of day and compute diffs
            mins = [h * 60 + m for h, m in times]
            for i in range(1, len(mins)):
                diff = mins[i] - mins[i - 1]
                if diff < 0:
                    continue
                name = f"Fermata {len(stops) + 1}"
                stops.append({"name": name, "travel_minutes_from_prev": diff})
            last_time = mins[-1]
        elif len(times) == 1 and last_time is not None:
            cur = times[0][0] * 60 + times[0][1]
            diff = cur - last_time
            if diff >= 0:
                name = f"Fermata {len(stops) + 1}"
                stops.append({"name": name, "travel_minutes_from_prev": diff})
            last_time = cur

    # Fallback: if nothing parsed but there are numbered bullets
    if not stops:
        bullet = re.compile(r"^(?:\d+\.|[-•])\s*(.+)$")
        for l in lines:
            b = bullet.match(l)
            if b:
                name = b.group(1).strip()
                stops.append({"name": name, "travel_minutes_from_prev": 3})

    # Ensure first stop has 0 minutes (from previous)
    if stops:
        stops[0]["travel_minutes_from_prev"] = 0

    return stops


@app.post("/api/bus/parse-image")
async def parse_image(file: UploadFile = File(...)):
    content = await file.read()
    api_key = os.getenv("OCR_SPACE_API_KEY")

    extracted_text = None

    if api_key:
        try:
            resp = requests.post(
                "https://api.ocr.space/parse/image",
                headers={"apikey": api_key},
                data={"language": "ita", "OCREngine": 2},
                files={"file": (file.filename, content, file.content_type or "image/jpeg")},
                timeout=30,
            )
            data = resp.json()
            if data.get("IsErroredOnProcessing"):
                raise RuntimeError(data.get("ErrorMessage") or "OCR error")
            parsed_results = data.get("ParsedResults") or []
            if parsed_results:
                extracted_text = "\n".join([r.get("ParsedText", "") for r in parsed_results])
        except Exception as e:
            # Will fallback to empty
            extracted_text = None

    if not extracted_text:
        # Graceful fallback: return message prompting CSV/text upload
        return {
            "text": "",
            "stops": [],
            "note": "OCR non disponibile. Fornisci una chiave OCR_SPACE_API_KEY nell'ambiente oppure carica un file testo/CSV con fermate e tempi (es. 'Duomo - 5 min')."
        }

    stops = parse_text_to_stops(extracted_text)
    return {"text": extracted_text, "stops": stops}


@app.get("/api/bus/lines/{line_id}/eta")
def compute_eta(line_id: str, start_time: Optional[str] = None):
    # start_time as HH:MM, default now
    from datetime import datetime, timedelta
    try:
        doc = db[BUS_COLLECTION].find_one({"_id": ObjectId(line_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="ID non valido")
    if not doc:
        raise HTTPException(status_code=404, detail="Linea non trovata")

    now = datetime.now()
    if start_time:
        try:
            h, m = map(int, start_time.split(":"))
            base = now.replace(hour=h, minute=m, second=0, microsecond=0)
        except Exception:
            base = now
    else:
        base = now

    stops = doc.get("stops", [])
    result = []
    elapsed = 0
    for s in stops:
        elapsed += int(s.get("travel_minutes_from_prev", 0))
        eta = base + timedelta(minutes=elapsed)
        result.append({"name": s.get("name"), "eta": eta.strftime("%H:%M")})

    return {"line": to_serializable(doc), "etas": result}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
