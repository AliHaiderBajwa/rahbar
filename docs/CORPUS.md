# Corpus

**Source:** the FAST-NUCES public website (nu.edu.pk), public pages only: never SLATE,
Flex, NU Mail or anything behind a login. **Documents:** 9 (7 PDFs + 2 web pages).
**Pages:** 279 PDF pages + 2 HTML pages. **After cleaning:** 70,182 words (41 pages went through OCR).

We do not own these files, so the repository commits only `corpus/sources.yaml` (what to
fetch, with `doc_version` and `superseded` per document), `corpus/fetch.py` (the polite
downloader from the starter kit, plus a `--dest` option for the Job) and
`corpus/MANIFEST.md` (every URL with its size and SHA-256). `corpus/raw/` is gitignored.
In the cluster, the indexer Job's `fetch-corpus` init container downloads the documents
into an `emptyDir` and compares each SHA-256 against the committed manifest
(PIPELINE.md, "How the documents reach the Job").

All nine URLs were checked to download on 7 November 2026, including the 2023 rules that
the handout warned "would not open when checked".

| # | Document | File | Pages | `doc_version` | `superseded` |
| --- | --- | --- | ---: | --- | --- |
| 1 | Undergraduate Academic Rules (Aug 2023) | `undergraduate-academic-rules-aug-2023.pdf` | 26 | 2023-08 | **true** |
| 2 | Academic Rules Handbook (Jun 2026) | `academic-rules-handbook-jun-2026.pdf` | 58 | 2026-06 | false |
| 3 | Prospectus 2026: Academic Rules and Regulations | `prospectus-2026-academic-rules-and-regulations.pdf` | 5 | 2026 | false |
| 4 | Student Handbook (Jun 2026) | `student-handbook-jun-2026.pdf` | 64 | 2026-06 | false |
| 5 | Annual Report 2019-20 | `annual-report-2019-20.pdf` | 33 | 2019-20 | false |
| 6 | Annual Report 2023-24 | `annual-report-2023-24.pdf` | 85 | 2023-24 | false |
| 7 | Fee Structure page | `fee-structure-page.html` | web | 2026-27 | false |
| 8 | Grading page | `grading-page.html` | web | 2026 | false |
| 9 | Faculty Entrepreneurship and Technical Start-ups Policy | `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` | 8 | 2022-05 | false |

## The seven required properties

| # | Required property | Document(s) that provide it | Evidence or notes |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | All nine | 9 documents, 279 PDF pages plus two web pages. |
| 2 | Two versions of one policy | **#1 Academic Rules 2023** (superseded) and **#2 Academic Rules Handbook 2026** (current); #3 restates the 2026 rules with different clause numbers | `superseded: true` on #1 in `sources.yaml` becomes chunk metadata; the retriever filters `superseded: false` unless the question asks for an older edition (`api/app/retrieval/chroma.py`, `wants_older_version`). Rules that really changed: the withdrawal deadline (2023 clause 4.25 "the last day of classes", 2026 clause 4.25 "the last day of the makeup week"), the regular semester (2023 1.6 "15/16 weeks of teaching", 2026 1.6 "16 weeks of teaching, one makeup week") and summer (2023 "8 to 9 weeks", 2026 6.1 "an 8-week semester"). Golden questions v01-v03. |
| 3 | Scanned, image-only PDF | **#9 Faculty Entrepreneurship policy** (all 8 pages) and, which we did not expect, **#1 Academic Rules 2023** (24 of 26 pages) | `docs/evidence/pdffonts-scanned-pdfs.txt`: `pdffonts` lists **no fonts at all** for #9; #1 has fonts only on its cover pages. pdfplumber returns fewer than 25 characters on those pages, so `extract.py` renders them at 200 dpi and OCRs them with tesseract (41 pages in total, including 9 image-only cover pages of the 2026 handbooks; about 8 minutes on our laptop). Because #1 is half of the version pair, OCR is not optional for us: without it the superseded edition is empty, and the version filter has nothing to filter. |
| 4 | A table with real information | **#7 Fee Structure page** (tuition per credit hour, miscellaneous fees, refund rules), **#8 Grading page** and handbook clause 2.3 (letter grade → grade points), **#5/#6 Annual Reports** (graduates by campus and programme, faculty by campus) | HTML tables become pipe-delimited rows; PDF tables found by pdfplumber are rendered as rows at their position on the page. The annual reports' "Graduation Summary" is drawn without ruling lines, so pdfplumber does not see a table and its columns come out as space-separated text, with one empty cell lost (PIPELINE.md has the before/after). Golden g07, g08, g11, e02. |
| 5 | Long definitions | **#4 Student Handbook** and **#2 Handbook** chapter 10: the Generative AI policy (numbered sections 1-12, with 6.1 non-lab and 6.2 lab rules running over a page each); the annual reports' narrative chapters | A fixed 100-word window cuts the lab rules away from their "6.2 Use in Lab Courses" heading; the structure-aware chunker keeps each sub-section together and labels it (ADR 0002). Golden g06. |
| 6 | A deliberately absent topic | **Hostel and transport rules.** We left out the prospectus's *Transport and Hostel Facilities* PDF (`https://nu.edu.pk/Admissions/ProspectusUpload/Transport and Hostel Facilities.pdf`), listed but commented out in `sources.yaml` | Students ask about hostels all the time, and the corpus *nearly* covers it: the annual reports say the Chiniot-Faisalabad campus has "Boys and Girls Hostel" facilities, so retrieval scores for hostel questions are high (0.74). That makes this a real test: the relevance floor cannot catch it, and the model has to notice that "hostels exist" does not answer "how do I apply and what does it cost". Golden a01, a02 must abstain. |
| 7 | Exact-term content | Clause numbers (1.21, 4.24, 5.4), CGPA thresholds (2.00, 3.90, 4.00), fees (Rs 30,000 admission, Rs 12,000 per credit hour), course names (Programming Fundamentals) | The same rule carries different numbers in different documents: attendance is clause 1.21 in #1 and #2 but section "1. Attendance Requirement" in #3. Golden e01-e03. The hybrid retriever's lexical leg exists for these (PIPELINE.md, ENGINEERING-NOTES question 5). |

## More traps we found in the documents
* **Hidden duplicate text (#2, #4).** The two 2026 handbooks were exported from a print
  imposition: each PDF page also carries the neighbouring page's text at negative x
  coordinates, outside the visible page. pdfplumber reads it, so every clause appeared
  twice, interleaved line by line with another page. We clip every page to its own
  bounding box (`extract.py`). Word count for #2 fell from about 15,800 to 9,212.
* **Page frames read as tables (#3).** The prospectus draws a border round each page;
  pdfplumber reported each page as a one-cell table holding all three text columns.
* **Letter-spaced justified text (#3):** `t u t o r i a l s`. Re-joined in `clean.py`.
* **Two answers to one question inside the 2026 documents.** The 2026 handbook (1.21)
  requires 100% attendance with up to 20% condonable; the 2026 prospectus says the
  "minimum attendance requirement for undergraduate degree programs is 80%". These are
  consistent readings of one rule, and the injection question i01 lists both as relevant.
