import os
import glob
import pdfplumber
from elasticsearch import Elasticsearch, helpers
from dotenv import load_dotenv

load_dotenv()

ELASTIC_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
ELASTIC_API_KEY = os.getenv("ELASTIC_API_KEY")
ELASTIC_INDEX = os.getenv("ELASTIC_INDEX", "awa-link-sr")

# Elasticsearchクライアントの初期化
if ELASTIC_API_KEY:
    es = Elasticsearch(ELASTIC_URL, api_key=ELASTIC_API_KEY)
else:
    # 認証情報なし、または基本認証用のフォールバック
    ELASTIC_USER = os.getenv("ELASTIC_USER")
    ELASTIC_PASSWORD = os.getenv("ELASTIC_PASSWORD")
    if ELASTIC_USER and ELASTIC_PASSWORD:
        es = Elasticsearch(ELASTIC_URL, basic_auth=(ELASTIC_USER, ELASTIC_PASSWORD))
    else:
        es = Elasticsearch(ELASTIC_URL)

def create_index():
    if es.indices.exists(index=ELASTIC_INDEX):
        print(f"Index {ELASTIC_INDEX} already exists. Skipping creation.")
        return

    print(f"Creating index: {ELASTIC_INDEX}")
    # kuromojiアナライザーを試みる
    try:
        es.indices.create(
            index=ELASTIC_INDEX,
            body={
                "settings": {
                    "analysis": {
                        "analyzer": {
                            "ja_analyzer": {
                                "type": "custom",
                                "tokenizer": "kuromoji_tokenizer",
                                "filter": [
                                    "kuromoji_baseform",
                                    "kuromoji_part_of_speech",
                                    "cjk_width",
                                    "stop",
                                    "ja_stop",
                                    "kuromoji_stemmer"
                                ]
                            }
                        }
                    }
                },
                "mappings": {
                    "properties": {
                        "filename": {"type": "keyword"},
                        "page": {"type": "integer"},
                        "text": {"type": "text", "analyzer": "ja_analyzer"}
                    }
                }
            }
        )
        print("Created index with Japanese (kuromoji) analyzer.")
    except Exception as e:
        print(f"Failed to create index with kuromoji analyzer: {e}")
        print("Falling back to standard analyzer...")
        # フォールバックとしてstandardアナライザーを使用
        es.indices.create(
            index=ELASTIC_INDEX,
            body={
                "mappings": {
                    "properties": {
                        "filename": {"type": "keyword"},
                        "page": {"type": "integer"},
                        "text": {"type": "text", "analyzer": "standard"}
                    }
                }
            }
        )
        print("Created index with standard analyzer.")

def extract_text_from_pdf(pdf_path):
    documents = []
    print(f"Extracting text from {pdf_path}...")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if text:
                    documents.append({
                        "filename": os.path.basename(pdf_path),
                        "page": page_num,
                        "text": text
                    })
    except Exception as e:
        print(f"Error reading {pdf_path}: {e}")
    return documents

def ingest_pdfs():
    pdf_dir = "data/pdfs"
    os.makedirs(pdf_dir, exist_ok=True)
    
    pdf_files = glob.glob(os.path.join(pdf_dir, "*.pdf"))
    if not pdf_files:
        print(f"\n[INFO] No PDF files found in {pdf_dir}.")
        print("Please place your systematic review (SR) PDFs in that directory and run this script again.")
        print(f"Directory path: {os.path.abspath(pdf_dir)}")
        return

    create_index()

    actions = []
    for pdf_path in pdf_files:
        docs = extract_text_from_pdf(pdf_path)
        for doc in docs:
            action = {
                "_index": ELASTIC_INDEX,
                "_source": {
                    "filename": doc["filename"],
                    "page": doc["page"],
                    "text": doc["text"]
                }
            }
            actions.append(action)
    
    if actions:
        print(f"Ingesting {len(actions)} pages into Elasticsearch...")
        helpers.bulk(es, actions)
        print("Ingestion complete successfully!")
    else:
        print("No content could be extracted from the PDFs.")

if __name__ == "__main__":
    ingest_pdfs()
