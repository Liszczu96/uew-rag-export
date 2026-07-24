# UEW RAG – paczka danych dla integracji

## 1. Cel

Repozytorium zawiera automatycznie aktualizowaną bazę wiedzy Uniwersytetu Ekonomicznego we Wrocławiu, przygotowaną do zaimportowania do systemu RAG.

Dane obejmują treści stron internetowych oraz tekst wydobyty z dokumentów powiązanych ze stronami UEW. Nie zawierają embeddingów ani konfiguracji konkretnego modelu językowego.

## 2. Główne pliki

### `public/rag/chunks.jsonl`

Podstawowy plik do indeksowania w systemie RAG.

Każdy wiersz jest osobnym obiektem JSON reprezentującym jedną spójną jednostkę informacji. Chunkowanie uwzględnia naturalne granice treści, m.in. sekcje, nagłówki, akapity, listy, zdania, strony dokumentów, slajdy i arkusze.

Nie jest stosowany sztuczny overlap między chunkami.

### `public/rag/corpus-documents.jsonl`

Pełne dokumenty przed podziałem na chunki. Plik może być wykorzystywany do ponownego chunkowania po stronie systemu docelowego.

### `public/rag/corpus-state.jsonl`

Stan dokumentów i ich hashe. Służy do rozpoznawania dokumentów nowych, zmienionych, usuniętych i niezmienionych.

### `public/changes/rag-corpus-changes.jsonl`

Lista zmian wykrytych podczas ostatniego uruchomienia pipeline’u.

### `public/rag/corpus-exclusions.jsonl`

Dokumenty pominięte wraz z przyczyną wykluczenia, np. brak wartościowej treści, dokument nieindeksowalny, niska jakość OCR lub decyzja o wyłączeniu z RAG.

### `public/rag-quality-status.json`

Końcowy raport audytu technicznego i jakościowego chunków.

## 3. Najważniejsze pola w `chunks.jsonl`

| Pole | Znaczenie |
|---|---|
| `id` | Stabilny, unikalny identyfikator chunka |
| `document_id` | Identyfikator dokumentu nadrzędnego |
| `document_kind` | Rodzaj źródła: `web` albo `attachment` |
| `source_id` | Główne źródło treści |
| `source_ids` | Wszystkie źródła powiązane z treścią |
| `source_priority` | Priorytet źródła |
| `title` | Tytuł dokumentu |
| `url` | Kanoniczny adres źródła |
| `url_aliases` | Alternatywne adresy tej samej treści |
| `language` | Wykryty język |
| `chunk_index` | Indeks chunka liczony od zera |
| `chunk_number` | Numer chunka liczony od jednego |
| `chunk_count` | Łączna liczba chunków dokumentu |
| `context_path` | Ścieżka sekcji lub nagłówków prowadzących do treści |
| `text` | Właściwa treść chunka |
| `embedding_text` | Tekst przygotowany do embeddingu: tytuł, kontekst i treść |
| `char_count` | Liczba znaków |
| `word_count` | Liczba słów |
| `text_sha256` | Hash treści chunka |
| `document_content_sha256` | Hash całego dokumentu |

## 4. Zalecany sposób importu

1. Odczytywać `chunks.jsonl` wiersz po wierszu.
2. Generować embedding na podstawie pola `embedding_text`.
3. Jako treść wyświetlaną użytkownikowi zachować pole `text`.
4. W bazie wektorowej przechowywać co najmniej: `id`, `document_id`, `title`, `url`, `source_id`, `source_priority`, `document_kind`, `context_path`, `text_sha256`.
5. Aktualizacje wykonywać przez porównanie stabilnych identyfikatorów i hashy.
6. Przy odpowiedzi asystenta prezentować użytkownikowi adres z pola `url` jako źródło.

## 5. Aktualizacja danych

Pipeline działa w GitHub Actions i automatycznie:

1. pobiera aktualne treści WordPress,
2. wykrywa dokumenty i załączniki,
3. wydobywa tekst,
4. wykonuje OCR dla zatwierdzonych skanów,
5. usuwa duplikaty i wyklucza treści niskiej jakości,
6. buduje dokumenty i chunki,
7. wykonuje audyt integralności,
8. zapisuje zmiany w repozytorium.

Workflow główny:

`Daily UEW RAG pipeline`

## 6. Źródła danych

Podstawowe serwisy:

- `https://uew.pl`
- `https://rekrutacja.uew.pl`
- `https://transferwiedzy.uew.pl`
- `https://bon.uew.pl`
- `https://sd.uew.pl`

Uwzględniane są również dokumenty linkowane z innych instytucjonalnych subdomen UEW.

## 7. Technologie

- Python 3.12
- WordPress REST API
- Git i GitHub
- GitHub Actions
- `requests`
- `BeautifulSoup`
- `PyYAML`
- `pypdf`
- `python-docx`
- `python-pptx`
- `openpyxl`
- `odfpy`
- `antiword`
- Tesseract OCR
- Poppler
- JSON, JSONL i skompresowane pliki `.json.gz`
- SHA-256 do wersjonowania, deduplikacji i kontroli zmian

## 8. Aktualny stan

- 2549 dokumentów,
- 6831 chunków,
- 1592 dokumenty WWW,
- 957 zaakceptowanych załączników,
- 0 błędów strukturalnych w końcowym audycie.

Ostrzeżenia jakościowe dotyczą przede wszystkim technicznych nazw plików i tytułów. Nie blokują importu do systemu RAG, ale mogą zostać dodatkowo ujednolicone po stronie systemu docelowego.
