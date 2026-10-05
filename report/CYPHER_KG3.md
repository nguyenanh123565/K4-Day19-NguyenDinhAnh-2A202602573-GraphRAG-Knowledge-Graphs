# Cypher KG-3 — thử thủ công trong Neo4j Browser

Chưa chạy các truy vấn này. Chỉ có MATCH/RETURN, không ghi dữ liệu. Các query dưới đây chép từ constants trong src/graph.py.

## 1. Đặt tham số

Sửa q/doc_ids theo câu hỏi và chunk thực tế. Không dùng id node tự suy đoán. ids là elementId, case_ids là property Case.id; hai loại không thay thế nhau.

```text
:params {q: "Câu hỏi của bạn", doc_ids: [], ids: [], case_ids: [], substances: [], aggregate: false, maximum: false, article_ids: [], definition_pattern: "", limit: 12}
```

- substances: tên canonical trong graph, lấy từ câu hỏi; để [] nếu không có.
- aggregate: true khi hỏi danh sách vụ có liên quan chất.
- maximum: true khi hỏi mức tù tối đa/cao nhất.
- article_ids: số điều kèm luật nếu câu hỏi chỉ định, ví dụ cấu trúc "Điều <số> BLHS".
- definition_pattern: regex Java cho câu định nghĩa: bắt đầu số khoản, tên khái niệm rồi "là". Bật (?is), cho phép whitespace giữa từ. Để "" nếu không hỏi định nghĩa.
- LIMIT 12 là giới hạn context, không phải bảo đảm danh sách toàn corpus đầy đủ khi có nhiều hơn 12 vụ.

## 2. Lấy seed elementIds

```cypher
MATCH (n)
WHERE n.doc_id IN $doc_ids
   OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
   OR any(alias IN coalesce(n.aliases, []) WHERE size(alias) >= 3 AND toLower($q) CONTAINS toLower(alias))
RETURN collect(elementId(n)) AS ids;
```

Copy danh sách ids vào tham số ids bằng :param ids => [...] (giữ các tham số còn lại).

## 3. Xác định vụ để mở rộng

```cypher
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
```

Copy các giá trị case_id trả về thành danh sách case_ids: :param case_ids => [...].

Query giới hạn các vụ của người có tên/alias xuất hiện trong câu hỏi khi tìm được người đó. Nếu không hỏi người cụ thể, mở rộng từ seed và từ Article/Clause qua Crime. Nhánh aggregate tìm Case theo Substance trên toàn KB, rồi giới hạn kết quả.

## 4. Multi-hop qua Crime sang Article và Clause

```cypher
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
```

Đường đi: Case → CHARGED_WITH → Crime ← DEFINES ← Article → HAS_CLAUSE → Clause. Crime là node dùng chung của luật và tin. Nếu có tội riêng của người đang hỏi, chỉ chọn đúng Crime đó.

Chọn khoản 1; với câu thường, thêm khoản MENTIONS chất mà vụ INVOLVES, ưu tiên chất đã nêu trong câu hỏi. Với maximum, thêm khoản có số lớn nhất trong các khoản có penalty chứa "tù". Đây là heuristic phù hợp các điều BLHS trong corpus (khung tăng theo khoản), không phải bộ so sánh hình phạt tổng quát. Không chọn khoản tiền phạt bổ sung chỉ vì số khoản cao nhất.

Không chạy query này trong nhánh hỏi định nghĩa hoặc aggregation thuần; context() dùng query riêng bên dưới hoặc tóm tắt vụ.

## 5. Truy vấn luật trực tiếp

```cypher
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
```

Dùng cho Article/Clause seed hoặc điều được câu hỏi chỉ định; definition_pattern có thể tìm khoản định nghĩa ngay cả khi vector top-k bỏ sót. Không hardcode khái niệm hoặc số khoản. Khi hỏi định nghĩa, chỉ lấy khoản có cấu trúc định nghĩa phù hợp, thay vì toàn bộ điều. Không chạy nhánh này khi hỏi aggregation thuần.

## 6. Đọc kết quả và fallback

context() format text thành "[Article.id - title] khoản number: text", thêm tóm tắt Case và giữ seed facts. Kết quả luôn là list[str], loại trùng và tối đa max_facts; dành chỗ cho nội dung luật tránh seed facts lấp hết context.

Nếu không có path hoặc truy vấn mở rộng lỗi: giữ các seed facts đã có, logging cảnh báo khi lỗi. Nếu seed_facts lỗi ngay từ đầu: trả [], KG-4 vẫn ghép retrieved chunks và question vào prompt. Fallback không xác nhận Cypher đã chạy đúng: lỗi truy vấn cần kiểm tra qua cảnh báo và bước 4/5.
