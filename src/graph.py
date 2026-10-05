"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {id, name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {id, name, aliases, doc_id})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)

Case/Person keys follow report/ONTOLOGY.md. Shared Crime/Substance/Location nodes
carry their first source in doc_id and retain every contributing source in doc_ids.
Document nodes record source provenance independently of extraction success; they
do not assert a case, charge, or cross-KB link when the LLM fails.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")
LOGGER = logging.getLogger(__name__)

# Read-only KG-3 queries; manual Browser parameters are in report/CYPHER_KG3.md.
_CASES_CONTEXT_QUERY = """
MATCH (k:Case)
WHERE (elementId(k) IN $ids
   OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
   OR EXISTS {
       MATCH (k)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)
       WHERE elementId(a) IN $ids
          OR EXISTS { MATCH (a)-[:HAS_CLAUSE]->(cl:Clause) WHERE elementId(cl) IN $ids }
   }
   OR ($aggregate AND EXISTS {
       MATCH (k)-[:INVOLVES]->(sub:Substance) WHERE sub.name IN $substances
   }))
  AND (
      NOT EXISTS {
          MATCH (named:Person)
          WHERE toLower($q) CONTAINS toLower(named.name)
             OR any(alias IN coalesce(named.aliases, []) WHERE toLower($q) CONTAINS toLower(alias))
      }
      OR EXISTS {
          MATCH (named:Person)-[:INVOLVED_IN]->(k)
          WHERE toLower($q) CONTAINS toLower(named.name)
             OR any(alias IN coalesce(named.aliases, []) WHERE toLower($q) CONTAINS toLower(alias))
      }
  )
WITH k, CASE WHEN EXISTS {
    MATCH (p:Person)-[:INVOLVED_IN]->(k)
    WHERE toLower($q) CONTAINS toLower(p.name)
       OR any(alias IN coalesce(p.aliases, []) WHERE toLower($q) CONTAINS toLower(alias))
} THEN 0 ELSE 1 END AS priority
RETURN k.id AS case_id, k.name AS name, k.summary AS summary, k.date AS date
ORDER BY priority, case_id
LIMIT $limit
"""

_LEGAL_CONTEXT_QUERY = """
MATCH (k:Case)-[:CHARGED_WITH]->(crime:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
WHERE (elementId(k) IN $ids
    OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
    OR EXISTS {
        MATCH (a)-[:HAS_CLAUSE]->(seed_clause:Clause)
        WHERE elementId(a) IN $ids OR elementId(seed_clause) IN $ids
    })
  AND k.id IN $case_ids
  AND (
      NOT EXISTS {
          MATCH (p:Person)-[r:INVOLVED_IN]->(k)
          WHERE coalesce(r.charge, '') <> ''
            AND (toLower($q) CONTAINS toLower(p.name)
                 OR any(alias IN coalesce(p.aliases, []) WHERE toLower($q) CONTAINS toLower(alias)))
      }
      OR EXISTS {
          MATCH (p:Person)-[r:INVOLVED_IN]->(k)
          WHERE r.charge = crime.name
            AND (toLower($q) CONTAINS toLower(p.name)
                 OR any(alias IN coalesce(p.aliases, []) WHERE toLower($q) CONTAINS toLower(alias)))
      }
  )
  AND (
      cl.number = 1
      OR (NOT $maximum AND EXISTS {
          MATCH (k)-[:INVOLVES]->(sub:Substance)<-[:MENTIONS]-(cl)
          WHERE size($substances) = 0 OR sub.name IN $substances
      })
      OR ($maximum AND toLower(coalesce(cl.penalty, '')) CONTAINS 'tù'
          AND NOT EXISTS {
              MATCH (a)-[:HAS_CLAUSE]->(higher:Clause)
              WHERE higher.number > cl.number
                AND toLower(coalesce(higher.penalty, '')) CONTAINS 'tù'
          })
  )
RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number,
                cl.text AS text, cl.penalty AS penalty
ORDER BY article_id, number
LIMIT $limit
"""

_DIRECT_CONTEXT_QUERY = """
MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
WHERE (elementId(a) IN $ids OR elementId(cl) IN $ids
    OR a.id IN $article_ids
    OR ($definition_pattern <> '' AND cl.text =~ $definition_pattern))
  AND (
      ($definition_pattern <> '' AND cl.text =~ $definition_pattern)
      OR ($definition_pattern = '' AND (
          cl.number = 1
          OR (NOT $maximum AND EXISTS {
              MATCH (cl)-[:MENTIONS]->(sub:Substance) WHERE sub.name IN $substances
          })
          OR ($maximum AND toLower(coalesce(cl.penalty, '')) CONTAINS 'tù'
              AND NOT EXISTS {
                  MATCH (a)-[:HAS_CLAUSE]->(higher:Clause)
                  WHERE higher.number > cl.number
                    AND toLower(coalesce(higher.penalty, '')) CONTAINS 'tù'
              })
      ))
  )
RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number,
                cl.text AS text, cl.penalty AS penalty
ORDER BY article_id, number
LIMIT $limit
"""



def _text(value: Any) -> str:
    """Clean strings without inventing text for nulls or structured values."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def _normalize_name(value: str) -> str:
    return _text(value).lower()


def _items(value: Any) -> list:
    return value if isinstance(value, list) else []

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = _normalize_name(name).strip("\"'“”").strip()
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    query = normalize(_text(name))
    if not query:
        return None
    canonical = {}
    for original in known:
        key = normalize(_text(original))
        if key:
            canonical.setdefault(key, original)
    if query in canonical:
        return canonical[query]
    matches = difflib.get_close_matches(query, list(canonical), n=1, cutoff=0.8)
    return canonical[matches[0]] if matches else None

def find_substances(text: str) -> list[str]:
    lowered = unicodedata.normalize("NFC", text).lower()
    # Avoid matching Amphetamine inside Methamphetamine.
    return [name for name in SUBSTANCES
            if re.search(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)", lowered)]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = _text(doc.metadata["article"])               # "Điều 251 BLHS"
    title = _text(doc.metadata["title"]).split(". ", 1)[-1]   # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "evidence": "trích nguyên văn câu đầu tiên trong bài xác định vụ này, dùng để sắp xếp vụ",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "identity_detail": "thông tin phân biệt có trong nguồn nếu hai người cùng vụ trùng họ tên; nếu không thì chuỗi rỗng",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.
Không gán tội của cả vụ cho mọi người. Giải quyết tên rút gọn/biệt danh về cùng người khi có chứng cứ.
Không suy diễn mức án từ khung luật. Giữ lượng đúng từng vụ, không cộng tổng với lượng từng lần thu giữ.
Các đoạn giới thiệu bài liên quan không thuộc vụ chính; không trích chúng thành vụ hoặc gán dữ kiện sang vụ chính.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], Any], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content,
    )
    try:
        response = llm_fn(prompt)
    except Exception as error:
        # Only extraction failures are skippable; database failures must propagate.
        # Log diagnostic fields only, never the full response, request, or API key.
        body = getattr(error, "body", None)
        details = body.get("error", body) if isinstance(body, dict) else {}
        details = details if isinstance(details, dict) else {}
        code = _text(getattr(error, "code", None)) or _text(details.get("code"))
        error_type = _text(getattr(error, "type", None)) or _text(details.get("type"))
        headers = getattr(getattr(error, "response", None), "headers", {}) or {}
        retry_after = _text(headers.get("retry-after"))
        LOGGER.warning(
            "Skipping news %s: LLM failed (%s; status=%s; code=%s; type=%s; retry_after=%s)",
            doc.id, type(error).__name__, getattr(error, "status_code", None),
            code or "unknown", error_type or "unknown", retry_after or "not provided",
        )
        return []
    if isinstance(response, str):
        # Some providers return a JSON code fence even when json_mode is requested.
        raw = response.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
        try:
            response = json.loads(fenced.group(1) if fenced else raw)
        except json.JSONDecodeError:
            LOGGER.warning("Skipping news %s: invalid JSON", doc.id)
            return []
    if not isinstance(response, dict) or not isinstance(response.get("cases"), list):
        LOGGER.warning("Skipping news %s: expected object with cases list", doc.id)
        return []

    cases = []
    for raw_case in response["cases"]:
        if not isinstance(raw_case, dict):
            LOGGER.warning("Skipping malformed case in %s", doc.id)
            continue
        case = {key: _text(raw_case.get(key))
                for key in ("name", "summary", "date", "location", "evidence")}
        case["charges"] = sorted({c for c in
            (link_entity(x, known_crimes) for x in _items(raw_case.get("charges")) if isinstance(x, str)) if c})
        case["people"] = []
        for raw_person in _items(raw_case.get("people")):
            if not isinstance(raw_person, dict) or not _text(raw_person.get("name")):
                continue
            person = {key: _text(raw_person.get(key))
                      for key in ("name", "role", "sentence", "identity_detail")}
            person["aliases"] = sorted({_text(x) for x in _items(raw_person.get("aliases")) if _text(x)})
            person["charge"] = link_entity(_text(raw_person.get("charge")), known_crimes) or ""
            case["people"].append(person)
        # An explicit individual charge is also evidence of a charge in this case.
        case["charges"] = sorted(set(case["charges"]) | {p["charge"] for p in case["people"] if p["charge"]})
        substances = {}
        for raw_substance in _items(raw_case.get("substances")):
            if not isinstance(raw_substance, dict):
                continue
            name = _text(raw_substance.get("name"))
            if not name:
                continue
            # Exact dictionary normalization only: do not fuzzy-link distinct chemicals.
            name = next((s for s in SUBSTANCES if _normalize_name(s) == _normalize_name(name)), name.lower())
            substance = substances.setdefault(name, {"name": name, "amounts": []})
            amount = _text(raw_substance.get("amount"))
            if amount and amount not in substance["amounts"]:
                substance["amounts"].append(amount)
        case["substances"] = [{"name": s["name"], "amount": "; ".join(sorted(s["amounts"]))}
                              for s in substances.values()]
        case["people"].sort(key=lambda p: (_normalize_name(p["name"]), p["identity_detail"],
                                          json.dumps(p, ensure_ascii=False, sort_keys=True)))
        case["substances"].sort(key=lambda s: s["name"])
        if not (case["name"] or case["summary"] or case["people"] or case["charges"] or case["substances"]):
            LOGGER.warning("Skipping empty case in %s", doc.id)
            continue
        if _normalize_name(case["location"]) in {"tp.hcm", "tp hcm", "tp. hcm", "thành phố hồ chí minh", "tp hồ chí minh"}:
            case["location"] = "TP.HCM"
        elif _normalize_name(case["location"]) in {"hà nội", "tp hà nội", "tp. hà nội", "thành phố hà nội"}:
            case["location"] = "Hà Nội"
        else:
            case["location"] = _normalize_name(case["location"])
        cases.append(case)
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "id"),
                           ("Substance", "name"), ("Person", "id"), ("Location", "name"),
                           ("Document", "id")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_document_source(self, doc: Document, kb: str, extraction_status: str) -> None:
        """Retain the real source even when no structured entities can be extracted."""
        self.run(
            """
            MERGE (source:Document {id: $doc_id})
            SET source.doc_id = $doc_id, source.kb = $kb, source.title = $title,
                source.source_url = $source_url, source.extraction_status = $status
            """,
            doc_id=doc.id, kb=kb, title=_text(doc.metadata.get("title")),
            source_url=_text(doc.metadata.get("source_url")), status=extraction_status,
        )

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime})
                SET c.doc_id = coalesce(c.doc_id, $doc_id)
                SET c.doc_ids = CASE WHEN $doc_id IN coalesce(c.doc_ids, [c.doc_id])
                    THEN coalesce(c.doc_ids, [c.doc_id]) ELSE coalesce(c.doc_ids, [c.doc_id]) + [$doc_id] END
                MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances |
                MERGE (sub:Substance {name: s})
                SET sub.doc_id = coalesce(sub.doc_id, $doc_id)
                SET sub.doc_ids = CASE WHEN $doc_id IN coalesce(sub.doc_ids, [sub.doc_id])
                    THEN coalesce(sub.doc_ids, [sub.doc_id]) ELSE coalesce(sub.doc_ids, [sub.doc_id]) + [$doc_id] END
                MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {id: $case_id})
              SET k.name = $name, k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc})
                SET l.doc_id = coalesce(l.doc_id, $doc_id)
                SET l.doc_ids = CASE WHEN $doc_id IN coalesce(l.doc_ids, [l.doc_id])
                    THEN coalesce(l.doc_ids, [l.doc_id]) ELSE coalesce(l.doc_ids, [l.doc_id]) + [$doc_id] END
                MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges |
                MERGE (c:Crime {name: crime})
                SET c.doc_id = coalesce(c.doc_id, $doc_id)
                SET c.doc_ids = CASE WHEN $doc_id IN coalesce(c.doc_ids, [c.doc_id])
                    THEN coalesce(c.doc_ids, [c.doc_id]) ELSE coalesce(c.doc_ids, [c.doc_id]) + [$doc_id] END
                MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances |
                MERGE (sub:Substance {name: s.name})
                SET sub.doc_id = coalesce(sub.doc_id, $doc_id)
                SET sub.doc_ids = CASE WHEN $doc_id IN coalesce(sub.doc_ids, [sub.doc_id])
                    THEN coalesce(sub.doc_ids, [sub.doc_id]) ELSE coalesce(sub.doc_ids, [sub.doc_id]) + [$doc_id] END
                MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {id: p.id})
                SET person.name = p.name, person.aliases = p.aliases, person.doc_id = $doc_id
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            case_id=case["id"], name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Expand seed facts through Crime, returning bounded readable facts only."""
        try:
            seed_ids, seed_facts = self.seed_facts(question, doc_ids)
        except Exception as error:
            LOGGER.warning("Graph seeds unavailable (%s); using chunk context only", type(error).__name__)
            return []
        limit = max(0, max_facts)
        seeds = list(dict.fromkeys(f for f in seed_facts if isinstance(f, str) and f))
        if not limit:
            return []

        q = _text(question)
        lowered = q.lower()
        substances = find_substances(q)
        maximum = bool(re.search(r"tối đa|cao nhất|nặng nhất", lowered))
        aggregate = bool(substances and re.search(r"những vụ|các vụ|vụ (?:án|việc) nào", lowered))
        definition = re.search(r"([^,;:?!]+?)\s+là\s+gì", lowered)
        term = definition.group(1).strip() if definition else ""
        definition_pattern = (r"(?is)^\s*\d+\.\s*" + re.escape(term).replace(r"\ ", r"\s+")
                              + r"\s+là\s+.*") if term else ""
        # Include the law name in Article.id to distinguish identical article numbers.
        article_numbers = re.findall(r"\bđiều\s+(\d+)\b", lowered)
        law = "Luật PCMT" if "phòng, chống ma túy" in lowered or "pcmt" in lowered else "BLHS"
        article_ids = [f"Điều {number} {law}" for number in article_numbers]

        def read(cypher: str, **params: Any) -> list[dict]:
            try:
                return self.run(cypher, ids=seed_ids, **params)
            except Exception as error:
                LOGGER.warning("Graph expansion unavailable (%s); retaining seed facts", type(error).__name__)
                return []

        cases = read(_CASES_CONTEXT_QUERY, q=q, substances=substances,
                     aggregate=aggregate, limit=min(limit, 12))
        case_ids = [row["case_id"] for row in cases if row.get("case_id")]
        clause_limit = min(limit, 12)
        legal = read(_LEGAL_CONTEXT_QUERY, case_ids=case_ids, q=q,
                     substances=substances, maximum=maximum, limit=clause_limit) if case_ids and not (term or aggregate) else []
        direct = read(_DIRECT_CONTEXT_QUERY, article_ids=article_ids, substances=substances,
                      maximum=maximum, definition_pattern=definition_pattern, limit=clause_limit) if not aggregate else []
        clause_facts = []
        for row in legal + direct:
            text = _text(row.get("text")) or _text(row.get("penalty"))
            if text:
                clause_facts.append(f"[{row.get('article_id', '')} - {row.get('title', '')}] "
                                    f"khoản {row.get('number', '')}: {text}")
        case_facts = [f"Vụ việc '{_text(row.get('name'))}' ({_text(row.get('date'))}): "
                      f"{_text(row.get('summary'))}" for row in cases]
        # Reserve space for legal evidence so a full 1-hop seed set cannot crowd it out.
        clauses = list(dict.fromkeys(clause_facts))[:clause_limit]
        remaining = limit - len(clauses)
        seed_budget = max(0, remaining - min(len(case_facts), remaining // 3))
        facts = seeds[:seed_budget] + clauses + case_facts + seeds[seed_budget:]
        return list(dict.fromkeys(facts))[:limit]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph using the ontology in report/ONTOLOGY.md.

    llm_fn(prompt, json_mode=False) returns JSON text (or a decoded dict).
    Shared nodes retain their first source in doc_id and all sources in doc_ids.
    Source Document nodes keep doc_id even if news extraction fails or is empty.
    No reset here: the caller owns the database lifecycle.
    """
    graph.suggested_constraints()
    articles = []
    for doc in law_docs:
        article = parse_law_article(doc)
        graph.add_law_article(article)
        graph.add_document_source(doc, "law", "extracted")
        articles.append(article)
    crimes = sorted({article["crime"] for article in articles if article["crime"]})

    for doc in news_docs:
        # Persist provenance BEFORE the skippable LLM call. Do not invent a Case
        # or a Crime to make an unavailable extraction appear successful.
        graph.add_document_source(doc, "news", "pending")
        cases = extract_news_cases(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes)
        if not cases:
            graph.add_document_source(doc, "news", "no_structured_cases")
            LOGGER.warning("News %s retained as a source only; extraction empty or failed, no bridge asserted", doc.id)
            continue
        source = _normalize_name(doc.content)

        def case_order(case: dict) -> tuple:
            evidence = _normalize_name(case["evidence"])
            position = source.find(evidence) if evidence else -1
            if position < 0:
                positions = [source.find(_normalize_name(p["name"])) for p in case["people"]]
                position = min((p for p in positions if p >= 0), default=len(source))
            # Deterministic tie breaker independent of LLM list ordering.
            signature = json.dumps(case, ensure_ascii=False, sort_keys=True)
            return position, signature

        seen = set()
        ordered = []
        for case in sorted(cases, key=case_order):
            signature = json.dumps({k: v for k, v in case.items() if k != "evidence"},
                                   ensure_ascii=False, sort_keys=True)
            if signature not in seen:
                seen.add(signature)
                ordered.append(case)

        for index, case in enumerate(ordered, start=1):
            case["id"] = f"{doc.id}:case:{index}"
            people = {}
            # Resolve an explicit alias only if it names exactly one full-name entry.
            alias_targets = {}
            for person in case["people"]:
                for alias in person["aliases"]:
                    alias_targets.setdefault(_normalize_name(alias), set()).add(person["name"])
            for person in case["people"]:
                name_key = _normalize_name(person["name"])
                targets = alias_targets.get(name_key, set())
                if len(targets) == 1 and not person["identity_detail"]:
                    person["name"] = next(iter(targets))
                    name_key = _normalize_name(person["name"])
                detail = _normalize_name(person["identity_detail"])
                person["id"] = f"{case['id']}:person:{name_key}" + (f":{detail}" if detail else "")
                previous = people.get(person["id"])
                if previous is None:
                    people[person["id"]] = person
                else:
                    previous["aliases"] = sorted(set(previous["aliases"]) | set(person["aliases"]))
                    for key in ("role", "charge", "sentence"):
                        if previous[key] and person[key] and previous[key] != person[key]:
                            LOGGER.warning("Conflicting %s for person in %s; retaining first value", key, doc.id)
                        elif not previous[key]:
                            previous[key] = person[key]
            case["people"] = list(people.values())
            graph.add_news_case(case, doc)
        graph.add_document_source(doc, "news", "extracted")

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        """Use the same vector search as Flat RAG, augmented with graph facts."""
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(chunk["metadata"]["doc_id"] for chunk in chunks))
        facts = self.graph.context(question, doc_ids)
        chunk_context = "\n\n".join(
            f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1)
        )
        prompt = GRAPH_PROMPT.format(
            question=question, facts="\n".join(facts), chunks=chunk_context
        )
        return self.llm_fn(prompt)
