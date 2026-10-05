# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Đình Anh  **MSSV:** 2A202602573  **Ngày viết báo cáo:** 05/10/2026

Thông tin họ tên/MSSV suy từ tên thư mục project, cần đối chiếu trước khi nộp. Ngày trên là ngày viết báo cáo, không phải timestamp đo benchmark.

**Nguồn số liệu:** [ket_qua_benchmark_kg.txt](../ket_qua_benchmark_kg.txt), đủ Indexing, Querying và 12 câu trả lời của hai pipeline cho Q1–Q6. Đối chiếu đáp án và từ khóa với [benchmark_kg.json](../data/benchmark_kg.json), ontology với [ONTOLOGY.md](ONTOLOGY.md), yêu cầu với SUBMISSION.md và LAB_GUIDE.md. Không chạy lại benchmark/test hoặc sửa code khi viết báo cáo.

Cấu hình đúng theo dòng đầu file:
- Chat: `openrouter:openai/gpt-4o-mini`.
- Embedding: `openrouter:openai/text-embedding-3-small`.
- `top_k=3`, `chunk_size=800`, 176 chunks.
- Graph của lần đo: **247 nodes / 381 relationships**. File không có phân bố số node theo label hoặc timestamp; không suy ra những số liệu đó.
- Recall là tỉ lệ từ khóa xuất hiện trong câu trả lời, không phải độ chính xác pháp lý. Judge chấm 0/1/2; điểm trung bình không phải phần trăm câu đúng.

## 1. Chi phí (10 điểm)

Hai bảng dưới đây chép nguyên số liệu trong file benchmark:

```text
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112    105.6
graph       196     94878     5997   0.01054    204.5

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       47   0.00013     2.13
graph       0.86   1.67     3520       85   0.00057     3.33
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00112 | 0.01054 | ×9.41 |
| Indexing giây | 105.6 | 204.5 | ×1.94 |
| Mỗi câu: USD | 0.00013 | 0.00057 | ×4.38 |
| Mỗi câu: giây | 2.13 | 3.33 | ×1.56 |
| Mỗi câu: in_tok | 694 | 3520 | ×5.07 |
| Mỗi câu: out_tok | 47 | 85 | ×1.81 |

Tỉ lệ được tính từ số đã làm tròn trong file, ví dụ 0.00057 / 0.00013 = 4.3846; không khẳng định đây là tỉ lệ tính từ dữ liệu thô chưa làm tròn. Chi phí query tăng khoảng **338.46%**, thời gian query tăng khoảng **56.34%**; tuyệt đối tăng **0.00044 USD/câu** và **1.20 giây/câu**.

**Chi phí tăng thêm đến từ đâu?** Graph indexing gồm vector indexing giống Flat cộng dựng KG: tăng 20 calls, 38.806 input tokens, 5.997 output tokens, 0.00942 USD và 98.9 giây so với Flat. Theo src/graph.py, luật được parse deterministic, còn tin dùng LLM; phần tăng chủ yếu nằm ở extraction tin và thao tác dựng graph. Khi hỏi, prompt có thêm facts/khoản luật nên input tăng từ 694 lên 3520 tokens; số output cũng tăng từ 47 lên 85, cùng thời gian graph retrieval/generation làm chi phí và độ trễ query cao hơn.

USD trong file là ước tính được code metering tính, không phải hóa đơn provider. Theo bench_kg.py, LLM-as-judge gọi sau khi đã đo usage của câu trả lời nên chi phí judge không nằm trong bảng Querying; không dùng bảng này làm tổng tiền thực trả khi chạy --judge.

**Phân bổ theo số câu:** với một lần indexing và N câu có cơ cấu giống benchmark:
- Flat: `C_flat(N) ≈ 0.00112 + 0.00013 × N` USD.
- Graph: `C_graph(N) ≈ 0.01054 + 0.00057 × N` USD.
- Chênh lệch: `0.00942 + 0.00044 × N` USD, luôn dương với N ≥ 0.

Ví dụ N = 100: Flat khoảng 0.01412 USD, Graph khoảng 0.06754 USD, chênh 0.05342 USD. Đây là ngoại suy từ một lần đo, chưa gồm judge, vận hành Neo4j và cập nhật dữ liệu. Không có điểm hòa vốn tiền API để Graph rẻ hơn Flat trong mô hình này; lợi ích phải đến từ chất lượng câu trả lời và giảm công kiểm tra thủ công.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa chất lượng; Flat nhanh hơn | Cả hai định nghĩa đúng; Graph thêm Điều 2 khoản 4 |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa chất lượng; Flat nhanh hơn | Cả hai nêu đúng Trần Thanh Tuấn và Trần Minh Tâm |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Flat từ chối; Graph nối đủ án 36 tháng, tội, Điều 251 và khung 02–07 năm |
| Q4 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Flat từ chối; Graph nêu hành vi, Điều 255 khoản 4 và mức tối đa chung thân |
| Q5 | cross-kb-multi-hop | 0.60 / 1 | 0.80 / 1 | Graph hơn recall, chưa đúng đầy đủ | Graph nêu khoản 4 nhưng viện dẫn sai Điều 251 thay vì Điều 250 |
| Q6 | aggregation | 0.00 / 1 | 0.33 / 1 | Graph hơn recall, chưa đủ căn cứ thắng về chất lượng | Graph nêu đúng Cái Quang Huy, nhưng hai vụ còn lại thiếu định danh chuẩn |

| Câu | Flat: giây | Graph: giây |
| --- | --- | --- |
| Q1 | 2.35 | 2.77 |
| Q2 | 1.70 | 3.16 |
| Q3 | 2.38 | 3.22 |
| Q4 | 1.35 | 2.37 |
| Q5 | 2.77 | 3.84 |
| Q6 | 2.24 | 4.65 |

### Q1 — định nghĩa tiền chất

Flat trả đúng định nghĩa hóa chất không thể thiếu trong điều chế, sản xuất chất ma túy thuộc danh mục do Chính phủ ban hành. Graph trả cùng nội dung, thêm “Điều 2 Luật PCMT khoản 4”. Cả hai recall 1.00, judge 2; Graph tăng 0.42 giây nhưng không tăng điểm chất lượng. Flat đủ cho câu hỏi mà đáp án nằm trong một đoạn luật; graph có ích nếu cần chỉ rõ nguồn điều/khoản.

### Q2 — bị cáo lãnh án tử hình trong vụ hơn 36kg

Cả hai nêu đúng Trần Thanh Tuấn và Trần Minh Tâm. Flat còn nêu tuổi; Graph thêm tội mua bán và Điều 251. Câu hỏi chỉ yêu cầu ai lãnh án tử hình, nên phần bổ sung không tạo lợi thế về score: cả hai recall 1.00, judge 2. Graph mất 3.16 giây so với 1.70 giây của Flat.

### Q3 — nối mức án của Lê Minh Thành với luật

Flat trả nguyên văn “Không đủ thông tin.” Graph trả:
> Lê Minh Thành bị tuyên 36 tháng tù về tội mua bán trái phép chất ma túy. Tội này được quy định tại Điều 251 BLHS với khung hình phạt cơ bản từ 02 năm đến 07 năm tù.

Câu trả lời Graph khớp cả dữ kiện báo chí lẫn căn cứ luật trong gold. Recall tăng 0 → 1, judge tăng 0 → 2, thời gian tăng 2.38 → 3.22 giây. Đây là bằng chứng rõ cho giá trị của đường Person → Case → Crime ← Article → Clause; riêng file câu trả lời không cho biết chính xác các chunk/facts đã đưa vào prompt.

### Q4 — Hoàng Nato và khung phạt cao nhất

Flat cũng trả “Không đủ thông tin.” Graph nhận đúng hành vi tổ chức sử dụng trái phép chất ma túy, dẫn Điều 255 khoản 4 và “20 năm hoặc tù chung thân”. Recall 0 → 1, judge 0 → 2, thời gian 1.35 → 2.37 giây. Graph giải quyết được câu cần nối alias/người trong tin với khung luật; không gọi đây là mức án đã tuyên cho người bị bắt.

### Q5 — khối lượng MDMA và điều luật tương ứng

Flat nêu tội vận chuyển, MDMA hơn 9,6kg và khung 20 năm/chung thân/tử hình, nhưng gọi “khoản b)” thay vì phân biệt khoản 4 và điểm b; không nêu Điều 250, cũng bỏ Ketamine. Recall 0.60, judge 1 phản ánh câu trả lời một phần.

Graph nêu rõ khoản 4, ngưỡng MDMA 100g trở lên và khung phạt, nhưng viết **“khoản 4 của Điều 251 BLHS”**. Gold yêu cầu **Điều 250 BLHS** cho tội vận chuyển; Điều 251 quy định mua bán. Graph cũng bỏ Ketamine. Do đó recall tăng lên 0.80 không có nghĩa câu này đúng pháp lý; judge vẫn 1 và thời gian tăng 2.77 → 3.84 giây.

Cả hai điều có nhóm MDMA và khung cao nhất tương tự trong corpus nên đáp án sai điều vẫn có thể chứa nhiều từ khóa đúng. Chưa có dump graph/prompt để kết luận sai nằm ở extraction/linking, chọn facts hay generation. Src/graph.py có cả nhánh law retrieval trực tiếp và multi-hop: nguy cơ trộn khoản cùng chất nhưng khác tội là giả thuyết cần kiểm tra, không phải nguyên nhân đã xác nhận.

### Q6 — tổng hợp các vụ có MDMA

Flat liệt kê vụ của “Đức”, “Thành”, “Đông”, có mô tả viên MDMA nhưng không chứa ba định danh trong must_include: Cái Quang Huy, Lê Minh Thành, Pháp y tâm thần. Recall vì thế là 0 dù judge cho 1. Việc gọi “vụ của Đức” cũng có nguy cơ nhầm người nhận hàng với Cái Quang Huy là người bị cáo buộc vận chuyển; không xem toàn bộ câu trả lời này là đúng chỉ vì nhận ra một số ngữ cảnh.

Graph liệt kê vụ vận chuyển của Cái Quang Huy, vụ góp 14 triệu mua 5 viên MDMA và vụ ở Sầm Sơn. Chỉ tên Cái Quang Huy khớp must_include, nên recall 0.33; câu mô tả vụ góp tiền có thể nhận ra vụ Lê Minh Thành nhưng không nêu tên người, còn vụ Sầm Sơn không định danh rõ vụ Viện Pháp y tâm thần Trung ương. Phần cuối Graph nói không có số điều luật trong ngữ cảnh, trong khi câu hỏi không yêu cầu luật: đây là phần trả lời ngoài nhu cầu và không bù thiếu định danh vụ.

Judge của cả hai là 1; Graph chậm hơn 2.24 → 4.65 giây, dài hơn nhưng chưa thể hiện lợi thế tổng hợp rõ. Cần so danh sách Case–MDMA trong graph thật với output để phân biệt thiếu dữ liệu graph, retrieval cắt mất facts, LLM đổi tên vụ hay phép đo dùng từ khóa quá cứng.

## 3. Phân tích lỗi (20 điểm)

Hai nhóm có bằng chứng là **E4 (phép đo)** trong benchmark hiện tại và **E1 (cầu nối gãy do thiếu extraction)** trong log --check đã được người dùng cung cấp trước đó. Không gán một giả thuyết chưa kiểm chứng thành lỗi graph đã xác nhận.

### Lỗi E4: Recall cao dù sai căn cứ luật; recall thấp dù nhận ra một phần ngữ cảnh

**Hiện tượng:** Q5 Graph recall 0.80 nhưng chọn sai điều luật; Q6 Flat recall 0.00 dù judge 1 và có mô tả ba ngữ cảnh liên quan MDMA.

**Bằng chứng trong ket_qua_benchmark_kg.txt:**
> Q5 Graph: “Với khối lượng MDMA hơn 9,6kg trong vụ này, khoản áp dụng tương ứng là khoản 4 của Điều 251 BLHS”

Đây là đoạn trích nguyên văn của câu trả lời. Dòng score là `recall=0.80 judge=1`. Trong benchmark_kg.json, must_include của Q5 là `["vận chuyển", "MDMA", "Điều 250", "khoản 4", "tử hình"]`. Output có 4/5 từ khóa nhưng thiếu “Điều 250”; gold xác định đúng là Điều 250 khoản 4. Recall cộng từng từ khóa không phạt mâu thuẫn “vận chuyển” với “Điều 251”.

Q6 Flat có `recall=0.00 judge=1`, trong khi output chứa:
> Vụ việc của Thành liên quan đến 5 viên nén màu trắng được xác định là ma túy MDMA.

Must_include dùng “Lê Minh Thành”, nên tên rút gọn “Thành” không được tính. Q6 Graph tương tự: mô tả vụ góp 14 triệu không được tính như tên đầy đủ; chỉ “Cái Quang Huy” đạt 1/3 từ khóa.

**Nguyên nhân:** bench_kg.py tính recall bằng substring lowercase, chia số từ khóa xuất hiện cho tổng từ khóa. Không kiểm tra quan hệ tội–điều–khoản, không nhận diện alias/mô tả tương đương và không xử lý sự nhập nhằng tên người. Judge giúp nhìn ra thiếu/sai nhưng cũng chỉ cho điểm một phần, không là ground truth độc lập.

**Đề xuất sửa:** ở một phép đo bổ sung sau lab, kiểm tra bộ tội–điều–khoản bắt buộc cho Q5 và phạt dẫn sai điều; với Q6, đánh giá tập vụ đã định danh, cho phép alias/mô tả khi có chứng cứ chắc chắn. Báo cáo đồng thời recall, judge và review thủ công; không sửa bench_kg.py để tăng điểm lần nộp này. Đánh đổi: cần rubric/mapping duyệt tay hoặc thêm chi phí judge, phải tránh map tên rút gọn sang nhầm người.

### Lỗi E1: Cầu nối không hình thành khi bài vụ án chỉ được giữ dưới dạng nguồn

**Hiện tượng:** trong lần --check trước đây, bài có vụ án thật bị lỗi gọi LLM; graph giữ nguồn nhưng không có Case/tội danh trích từ bài, nên không có đường sang luật. Đây là lỗi coverage ở đầu extraction, không phải bằng chứng mọi Crime canonical MERGE sai.

**Bằng chứng lịch sử, nguyên văn log người dùng đã cung cấp:**

```text
Skipping news news-100260918080821054: LLM failed (RateLimitError)
News news-100260918080821054 retained as a source only; extraction empty or failed, no bridge asserted

[LỖI KG-2] Không có đường đi (<= 4 cạnh) nối node của KB luật với node của bài báo: cầu nối 2 KB bị gãy.
```

Bài này là vụ Lê Minh Thành mà Q3 yêu cầu nối với Điều 251; theo ONTOLOGY.md và corpus, đây là bài có vụ cụ thể, không phải bài hội nghị/tuyên truyền. Lỗi trên thuộc lần chạy lịch sử dùng OpenAI; benchmark hiện tại dùng OpenRouter và Q3 đã đúng. Không dùng log cũ để khẳng định cầu nối Q3 vẫn gãy trong graph 247 nodes/381 rels.

**Nguyên nhân đã xác định cho log lịch sử:** extract_news_cases bắt lỗi LLM và trả []; build_graph lưu Document có doc_id/status nhưng bỏ qua tạo Case/Person/Crime và cạnh nghiệp vụ. Source provenance đáp ứng giữ tài liệu nhưng không tự hình thành tri thức hoặc bridge. Lỗi extraction của một bài không crash toàn build; --check dùng đúng một bài tin nên không có bài khác bù đường nối.

**Đề xuất sửa:** phân biệt status extraction_failed, valid_no_cases và extracted, lưu danh sách doc_id/error code cần xử lý lại; khi lỗi API đã được giải quyết, trích lại các bài lỗi có vụ thật rồi kiểm tra Case → Crime ← Article. Không nối Document vào một Crime suy đoán chỉ để check pass. Đánh đổi: thêm theo dõi trạng thái và chi phí gọi lại LLM; cần giữ kết quả trích/mapping ổn định khi nhập lại.

**Đánh giá sáu log source-only hiện tại:** theo thông tin người dùng cung cấp, có 6/20 bài news (30%) được giữ dưới dạng nguồn. Đây là tỉ lệ bài chưa tạo dữ kiện có cấu trúc trong lần đó, **không phải tỉ lệ lỗi extraction đã xác nhận**. File benchmark không lưu sáu doc_id hoặc log chi tiết; từ thông báo “empty or failed” chưa phân biệt được:
- Bài không có vụ cụ thể: cases=[] là hành vi đúng theo prompt.
- Bài có vụ nhưng JSON sai, LLM/API lỗi hoặc LLM bỏ sót: có thể làm mất Case và bridge cần thiết.
- Một sự kiện được đưa tin ở bài khác: source-only của một bài chưa đồng nghĩa mất sự kiện trên toàn KG.

Vì vậy log là bằng chứng có source-only documents, phù hợp để điều tra E1/thiếu extraction nhưng không đủ kết luận cả sáu là cầu nối gãy, cũng không đủ chứng minh đó là nguyên nhân duy nhất của Q6. Số calls Graph indexing tăng 20 không tự cho biết sáu bài này lỗi API hay JSON hợp lệ rỗng.

Các truy vấn **chỉ đọc, chưa chạy** để kiểm chứng trên graph của lần benchmark:

```cypher
MATCH (d:Document)
WHERE d.kb = 'news' AND d.extraction_status = 'no_structured_cases'
RETURN d.doc_id, d.title, d.extraction_status
ORDER BY d.doc_id;
```

```cypher
MATCH (k:Case)
WHERE NOT EXISTS { MATCH (k)-[:CHARGED_WITH]->(:Crime) }
RETURN k.id, k.name, k.doc_id;
```

Query Case không thấy bài đã skip vì Case chưa tồn tại, nên cần cả query Document và đọc bài gốc. Chưa có kết quả Cypher để dán; không ghi số hàng dự đoán như kết quả thực chạy.

### Giới hạn bằng chứng cho E2/E3/E5/E6

Chưa xác nhận graph có đủ đúng Article–Clause nhưng retrieval bỏ mất nên không gán Q5 chắc chắn vào E2. Chưa có dump duplicate để claim E3, chưa có đối chiếu graph/output để claim E5, chưa có query r.charge rỗng để claim E6. Lỗi thiếu định danh trong output Q6 và sai điều trong output Q5 là có thật; vị trí sâu trong pipeline cần kiểm tra tiếp.

Để phân biệt E5/generation với linking hoặc retrieval ở Q5, có thể tự chạy:

```cypher
MATCH (p:Person {name:'Cái Quang Huy'})-[r:INVOLVED_IN]->(k:Case)
OPTIONAL MATCH (k)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)
RETURN p.name, r.charge, k.id, k.doc_id, c.name, a.id;
```

Để đối chiếu aggregation Q6:

```cypher
MATCH (k:Case)-[r:INVOLVES]->(:Substance {name:'MDMA'})
OPTIONAL MATCH (p:Person)-[:INVOLVED_IN]->(k)
RETURN k.id, k.name, k.doc_id, k.summary, r.amount,
       collect(DISTINCT p.name) AS people
ORDER BY k.doc_id, k.id;
```

Đây là đề xuất kiểm chứng, không có kết quả chạy và không dùng để giả định graph đã chứa đủ ba vụ.

## 4. Kết luận (5 điểm)

GraphRAG đáng tiền trong lần đo này khi câu hỏi phải nối **người/vụ trong tin với tội danh và điều/khoản luật**: Q3 và Q4 đều tăng recall 0 → 1, judge 0 → 2, trong khi Flat chỉ trả “Không đủ thông tin”. Đổi lại, chi phí query tăng từ khoảng 0.00013 lên 0.00057 USD/câu (×4.38), độ trễ 2.13 lên 3.33 giây (×1.56), và indexing tăng ×9.41 về USD. Nếu giá trị của việc trả đúng câu xuyên nguồn lớn hơn chi phí tăng thêm 0.00044 USD/câu và 1.20 giây, việc dựng KG có cơ sở.

Flat RAG đủ và hiệu quả hơn cho câu single-hop như Q1/Q2: hai pipeline cùng recall 1.00/judge 2, Graph không tăng score nhưng chậm hơn. Với dữ liệu cần kiểm chứng pháp lý chặt hoặc truy vấn liệt kê toàn corpus, chưa nên dựa riêng vào GraphRAG hiện tại: Q5 vẫn sai Điều luật dù recall 0.80, Q6 chỉ recall 0.33/judge 1. Cần hoàn thiện coverage extraction, kiểm tra linking/retrieval và định danh vụ trước khi coi khả năng multi-hop/aggregation là đáng tin cậy.

Trên sáu câu, recall trung bình tăng **0.43 → 0.86** (+0.43 điểm theo bảng làm tròn), judge tăng **1.00 → 1.67** (+0.67 điểm trên thang 0–2). Đây là kết quả một lần đo với bộ sáu câu, không phải bảo đảm cho mọi câu hoặc mọi lần chạy; không suy ra ý nghĩa thống kê hoặc độ chính xác tổng quát. Chất lượng tăng rõ nhất ở Q3/Q4, chưa giải quyết đầy đủ Q5/Q6.

**Không xét bonus +15:** dùng ontology gợi ý có chỉnh khóa/provenance; không có file benchmark ontology HINT riêng để so sánh trước/sau và chứng minh cải thiện do thay ontology. Không dùng chênh lệch Graph–Flat làm bằng chứng thắng ontology HINT.

## 5. Tự kiểm (5 điểm)

Các kết quả dưới đây được ghi **theo xác nhận của người dùng trong yêu cầu hiện tại**; không tự chạy lại và không bịa thời gian, số node/chi phí hoặc nội dung từng dòng stdout. Project chưa có file lưu transcript pytest/--check đầy đủ.

| Lệnh | Kết quả người dùng xác nhận |
| --- | --- |
| pytest tests/ -q | **48 passed** |
| python bench_kg.py --check | **Đủ 7 dòng [OK]** |

```text
Tóm tắt self-check theo thông tin người dùng:
pytest tests/ -q          → 48 passed
python bench_kg.py --check → đủ 7 [OK]
```

Đây là tóm tắt, không phải stdout được sao chép từ log. Để đáp ứng yêu cầu SUBMISSION.md “dán output”, cần bổ sung transcript gốc khi nộp. Kết quả check mới không phủ nhận lỗi RateLimit/cầu nối đã xảy ra ở lần lịch sử; hai lần chạy khác nhau.

**Ảnh Neo4j bắt buộc:**

| File | Yêu cầu | Trạng thái kiểm tra filesystem |
| --- | --- | --- |
| report/img/kg_count.png | Đếm node theo label, thấy query và Results overview | Chưa có; thư mục img hiện chỉ có .gitkeep |
| report/img/kg_cross_kb.png | Person → Case → Crime ← Article | Chưa có |
| report/img/kg_my_case.png | Vụ của một người tự chọn, không dùng Lê Minh Thành | Chưa có |

**Người dự kiến chọn cho kg_my_case.png:** Dương Minh Tuấn (alias Hoàng Nato), vì Q4 thể hiện đường sang Điều 255. Đây là đề xuất, chưa có ảnh hoặc xác nhận query trả về người này; cần chọn một Person thực có trong graph khi chụp.

**Tính nhất quán ontology:** src/graph.py hiện thêm Document làm node provenance (id/doc_id/kb/title/source_url/extraction_status), ngoài bảy label nghiệp vụ; ONTOLOGY.md chưa liệt kê label phụ trợ này trong bảng entity. Trước khi nộp cần đồng bộ bản thiết kế với graph thực theo SUBMISSION.md. Báo cáo chỉ ghi nhận chênh lệch, không sửa ontology/code hoặc tự suy phân bố label từ tổng 247 nodes.

## Vấn đề gặp phải (không tính điểm)

- Lần --check lịch sử: RateLimitError ở news-100260918080821054, ban đầu báo không có node doc_id của KB tin; sau khi lưu nguồn Document, báo không có đường xuyên KB. Log liên quan được trích ở E1; không dựng lại transcript thiếu.
- Việc đã làm trong code trước lần viết báo cáo: giữ doc_id/provenance ngay cả khi extraction lỗi, log chi tiết mã API và giữ skip để không dừng toàn build. Không có thay đổi code trong công việc viết báo cáo này.
- Theo file benchmark hiện tại, provider là OpenRouter và Q3/Q4 trả đúng. Không suy ra nguyên nhân/hành động cụ thể đã giải quyết RateLimit chỉ từ thay đổi tên provider.
- Còn tồn tại: Q5 sai Điều 251 thay vì 250; Q6 thiếu tên vụ/người chuẩn; sáu source-only documents cần phân loại hợp lệ/lỗi; chưa có raw graph/prompt và chưa có benchmark lặp lại để xác định nguyên nhân.
- Còn hồ sơ nộp: transcript tự kiểm gốc, ba ảnh Neo4j và bổ sung Document vào ontology. Không claim đã có ảnh, đã tự chạy lại hoặc đã đạt bonus.
