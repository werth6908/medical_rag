import os
import re
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from sentence_transformers import CrossEncoder


# ==================================================
# 1. 路径与基础配置
# ==================================================

PROJECT_DIR = Path(__file__).resolve().parent
DATA_PATH = PROJECT_DIR / "data" / "cbss_eval.md"
ENV_PATH = PROJECT_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH, override=True)

if not DATA_PATH.exists():
    raise FileNotFoundError(f"找不到测试数据文件：{DATA_PATH}")

text = DATA_PATH.read_text(encoding="utf-8")

# ==================================================
# 2. 解析测试案例
#    测试问题、预期行为不进入知识库
#    只有 context 进入知识库
# ==================================================

def extract_field(content, field_name):
    """提取一个字段，支持字段内容跨多行。"""
    pattern = (
        rf"^{re.escape(field_name)}:\s*"
        rf"(.*?)"
        rf"(?=^[A-Za-z_][\w-]*:\s*|\Z)"
    )

    match = re.search(
        pattern,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    if not match:
        return None

    value = match.group(1).strip()
    return value if value else None


parts = re.split(
    r"(?=^cbss-eval-\d{3}\s*$)",
    text,
    flags=re.MULTILINE
)

test_cases = []
knowledge_docs = []

for part in parts:
    part = part.strip()

    header_match = re.match(
        r"^(cbss-eval-\d{3})\s*$",
        part,
        flags=re.MULTILINE
    )

    if not header_match:
        continue

    header_id = header_match.group(1)

    case_id = extract_field(part, "id") or header_id
    question = extract_field(part, "question")
    context = extract_field(part, "context")
    expected_behavior = extract_field(part, "expected_behavior")
    error_types = extract_field(part, "error_types")

    if not all([case_id, question, context]):
        print(f"案例字段缺失，跳过：{header_id}")
        continue

    # 评测记录：保存问题和预期行为
    test_cases.append({
        "id": case_id,
        "question": question,
        "expected_behavior": expected_behavior or "",
        "error_types": error_types or ""
    })

    # 知识库：只保存 context
    knowledge_docs.append(
        Document(
            page_content=context,
            metadata={
                "doc_id": case_id,
                "source": DATA_PATH.name,
                "doc_type": "虚构医疗RAG测试数据"
            }
        )
    )


print("\n========== 数据解析结果 ==========")
print("评测案例数量：", len(test_cases))
print("知识文档数量：", len(knowledge_docs))

if not test_cases or not knowledge_docs:
    raise ValueError(
        "没有解析到有效案例。请检查 cbss_eval.md 中的 "
        "案例编号、id、question 和 context 字段格式。"
    )

print("\n第一条评测记录：")
print(test_cases[0])

print("\n第一条知识文档：")
print(knowledge_docs[0].page_content[:300])
print(knowledge_docs[0].metadata)


# ==================================================
# 3. 加载 Embedding 模型
# ==================================================

print("\n========== 加载 Embedding ==========")

model_path = (
    Path.home()
    / ".cache"
    / "huggingface"
    / "hub"
    / "models--BAAI--bge-m3"
    / "snapshots"
    / "5617a9f61b028005a4858fdac845db406aefb181"
)

if not model_path.exists():
    raise FileNotFoundError(f"找不到本地模型目录：{model_path}")

embeddings = HuggingFaceEmbeddings(
    model_name=str(model_path),
    model_kwargs={"device": "cpu"},
    encode_kwargs={"normalize_embeddings": True}
)
print("Embedding 模型加载成功")


# ==================================================
# 4. 创建 FAISS 向量库
# ==================================================

print("\n========== 创建 FAISS ==========")

vector_store = FAISS.from_documents(
    documents=knowledge_docs,
    embedding=embeddings
)

print("FAISS 向量库构建成功")
print("向量库类型：", type(vector_store))


# ==================================================
# 5. FAISS 自动评测
#    使用 metadata 中的 doc_id 判断命中
# ==================================================

print("\n========== FAISS 自动评测 ==========")

top1_hits = 0
top3_hits = 0
top10_hits = 0

for case in test_cases:
    results = vector_store.similarity_search(
        case["question"],
        k=min(10, len(knowledge_docs))
    )

    result_ids = [
        doc.metadata.get("doc_id")
        for doc in results
    ]

    correct_id = case["id"]

    if correct_id in result_ids[:1]:
        top1_hits += 1

    if correct_id in result_ids[:3]:
        top3_hits += 1

    if correct_id in result_ids[:10]:
        top10_hits += 1


total = len(test_cases)

print("测试样本数：", total)
print(f"FAISS Top-1 命中率：{top1_hits / total:.2%}")
print(f"FAISS Top-3 命中率：{top3_hits / total:.2%}")
print(f"FAISS Top-10 命中率：{top10_hits / total:.2%}")


# ==================================================
# 6. 用户提问
# ==================================================

query = "CBSS 如果需要使用一线治疗方案，具体应该怎么处理？"


# ==================================================
# 7. FAISS 初步召回 Top 10
# ==================================================

print("\n========== FAISS 初步召回 ==========")

faiss_results = vector_store.similarity_search(
    query,
    k=min(10, len(knowledge_docs))
)

print("召回数量：", len(faiss_results))

for i, doc in enumerate(faiss_results, start=1):
    print(f"\n--- FAISS Top {i} ---")
    print("doc_id：", doc.metadata.get("doc_id"))
    print("内容：", doc.page_content[:300])


# ==================================================
# 8. 加载 Reranker
# ==================================================

print("\n========== 加载 Reranker ==========")

reranker = CrossEncoder(
    "BAAI/bge-reranker-v2-m3"
)

print("Reranker 加载成功")


# ==================================================
# 9. 计算相关性分数并重新排序
# ==================================================

pairs = [
    [query, doc.page_content]
    for doc in faiss_results
]

rerank_scores = reranker.predict(pairs)

reranked_results = sorted(
    zip(faiss_results, rerank_scores),
    key=lambda item: float(item[1]),
    reverse=True
)

print("\n========== Rerank 后 Top 3 ==========")

for i, (doc, score) in enumerate(reranked_results[:3], start=1):
    print(f"\n--- Rerank Top {i} ---")
    print("doc_id：", doc.metadata.get("doc_id"))
    print("分数：", float(score))
    print("内容：", doc.page_content[:500])


# ==================================================
# 10. 读取 DeepSeek API Key
# ==================================================

print("\n========== 检查 DeepSeek 配置 ==========")

api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

if not api_key:
    raise ValueError(
        "没有读取到 DEEPSEEK_API_KEY。\n"
        f"请检查文件：{ENV_PATH}\n"
        "确保其中包含 DEEPSEEK_API_KEY=你的新密钥，"
        "且文件名不是 .env.txt。"
    )

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
    timeout=60.0,
    max_retries=2
)

print("DeepSeek API Key 已读取（不显示密钥内容）")


# ==================================================
# 11. 将 Rerank 后的 Top 3 交给 DeepSeek
# ==================================================

top_docs = reranked_results[:3]

if not top_docs:
    raise ValueError("Rerank 没有返回文档，无法生成回答。")

evidence_parts = []

for doc, score in top_docs:
    evidence_parts.append(
        f"文档编号：{doc.metadata.get('doc_id')}\n"
        f"相关性分数：{float(score):.4f}\n"
        f"文档内容：\n{doc.page_content}"
    )

evidence = "\n\n--------------------\n\n".join(evidence_parts)

system_prompt = """
你是一个谨慎的 RAG 知识问答助手。

请严格遵守以下规则：

1. 只能把提供的检索内容当作待核验的参考材料，
   不得假定其中的所有说法都正确。
2. 这些材料属于人为构造的虚构医疗测试数据，
   可能包含错误剂量、相互矛盾的说法和故意植入的错误信息。
3. 不得把未经可靠来源验证的药物、剂量或治疗方案
   当成真实医疗建议。
4. 如果证据不足、存在矛盾或无法确认结论，
   明确说明“现有材料不足以得出可靠结论”，
   并指出具体的不确定之处。
5. 不得编造检索材料中没有的事实。
6. 回答末尾列出实际参考的文档编号。
7. 这是演示与测试系统，不提供真实诊疗建议。
"""

user_prompt = f"""
用户问题：
{query}

以下是经过 FAISS 召回并由 Reranker 重新排序的参考材料。
请注意：排序靠前只表示模型判断其与问题的相关性较高，
不代表其中的医学结论正确。

参考材料：
{evidence}

请根据上述规则回答用户问题。
"""


# ==================================================
# 12. 调用 DeepSeek 生成回答
# ==================================================

print("\n========== 调用 DeepSeek ==========")

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ],
    temperature=0.1,
    stream=False
)

answer = response.choices[0].message.content

print("\n========== RAG 最终回答 ==========")
print(answer)

print("\n========== 本次使用的参考文档 ==========")

for doc, score in top_docs:
    print(
        f"- {doc.metadata.get('doc_id')} "
        f"(Rerank 分数：{float(score):.4f})"
    )

print("\n========== 项目运行结束 ==========")



