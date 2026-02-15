import logging
import os
import re
import sys
import json
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
import html as htmlmod

import requests
import pdfplumber
import yagmail

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

URL_SITE = "https://www.mprr.mp.br/servicos/diario"
NOME_ARQUIVO = "diario_oficial_mais_recente.pdf"

STATE_DIR = ".state"
STATE_FILE = os.path.join(STATE_DIR, "last_seen.json")

SEND_ONLY_IF_NEW = os.getenv("SEND_ONLY_IF_NEW", "true").strip().lower() in {"1", "true", "yes", "y"}
SEND_ONLY_IF_MATCH = os.getenv("SEND_ONLY_IF_MATCH", "false").strip().lower() in {"1", "true", "yes", "y"}


def load_state() -> Dict[str, Any]:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"Falha ao ler state ({STATE_FILE}): {e}")
    return {}


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    r.raise_for_status()
    logging.info(f"HTTP {r.status_code} | final_url={r.url} | content-type={r.headers.get('Content-Type')}")
    return r.text


def parse_timestamp_from_url(u: str) -> Optional[datetime]:
    # padrão: ...-YYYY-MM-DD-HH-MM-SS.pdf
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.pdf", u)
    if not m:
        return None
    y, mo, d, hh, mm, ss = map(int, m.groups())
    return datetime(y, mo, d, hh, mm, ss)


def parse_edicao_from_url(u: str) -> Optional[int]:
    m = re.search(r"-n-(\d+)-", u, flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def normalize_html_for_pdf_search(raw: str) -> str:
    """
    Normaliza escapes comuns quando URLs aparecem dentro de JS/JSON no HTML:
    - &amp; etc.
    - \/  (slashes escapados)
    - \u002F (slash unicode em JSON)
    """
    s = htmlmod.unescape(raw)
    s = s.replace("\\u002F", "/").replace("\\u002f", "/")
    s = s.replace("\\/", "/")
    return s


def extrair_links_pdf_do_html(raw_html: str) -> List[str]:
    """
    Extrai URLs de PDF (com foco no diário do MPRR) mesmo se estiverem escapadas em JS.
    Retorna URLs absolutas https://www.mprr.mp.br/...
    """
    html_norm = normalize_html_for_pdf_search(raw_html)

    urls = set()

    # 1) URLs relativas /servicos/download/...pdf
    for m in re.finditer(r"(/servicos/download/[^\"'<>\s]+?\.pdf)", html_norm, flags=re.IGNORECASE):
        urls.add("https://www.mprr.mp.br" + m.group(1))

    # 2) URLs absolutas https://www.mprr.mp.br/servicos/download/...pdf
    for m in re.finditer(r"(https?://www\.mprr\.mp\.br/servicos/download/[^\"'<>\s]+?\.pdf)", html_norm, flags=re.IGNORECASE):
        urls.add(m.group(1))

    # 3) Fallback bem amplo: qualquer coisa contendo .pdf e mprr (caso mude o caminho)
    if not urls:
        for m in re.finditer(r"([^\s\"'<>]+?\.pdf)", html_norm, flags=re.IGNORECASE):
            candidate = m.group(1)
            if "mprr.mp.br" in candidate:
                # garante esquema
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                elif candidate.startswith("/"):
                    candidate = "https://www.mprr.mp.br" + candidate
                urls.add(candidate)

    # filtra para o padrão do diário (se existir)
    filtradas = [u for u in urls if "diario" in u.lower() and "mprr" in u.lower()]

    # se o filtro ficou restritivo demais, usa o conjunto bruto
    final = filtradas if filtradas else list(urls)

    final_sorted = sorted(final)
    logging.info(f"Encontrados {len(final_sorted)} PDFs (após normalização).")
    if final_sorted:
        logging.info(f"Exemplo PDF: {final_sorted[-1]}")
    return final_sorted


def escolher_pdf_mais_recente(urls: List[str]) -> str:
    candidatos = []
    for u in urls:
        ts = parse_timestamp_from_url(u)
        ed = parse_edicao_from_url(u)
        candidatos.append((ts, ed, u))

    def key(x):
        ts, ed, u = x
        return (
            ts is None,
            ts or datetime.min,
            ed is None,
            ed or -1,
            u,
        )

    candidatos.sort(key=key)
    return candidatos[-1][2]


def baixar_pdf(url: str, destino: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=90, allow_redirects=True)
    r.raise_for_status()

    content_type = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        raise RuntimeError("Recebido HTML em vez de PDF (Content-Type text/html).")

    if not r.content.startswith(b"%PDF"):
        raise RuntimeError("Conteúdo baixado não parece PDF (header %PDF não encontrado).")

    with open(destino, "wb") as f:
        f.write(r.content)

    return r.content


def ler_texto_pdf(destino: str) -> str:
    partes = []
    with pdfplumber.open(destino) as pdf:
        for p in pdf.pages:
            partes.append(p.extract_text() or "")
    return "\n".join(partes)


def extrair_entre(texto: str, inicio: str, fim: str) -> Optional[str]:
    padrao = re.compile(re.escape(inicio) + r".*?" + re.escape(fim), flags=re.IGNORECASE | re.DOTALL)
    m = padrao.search(texto)
    return m.group(0) if m else None


def contem_concurso_publico(texto: str) -> bool:
    return re.search(r"concurso público", texto, flags=re.IGNORECASE) is not None


def enviar_email(assunto: str, corpo: str) -> None:
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASS")
    if not email_user or not email_pass:
        raise RuntimeError("EMAIL_USER/EMAIL_PASS não definidos (secrets).")

    destinatarios = ["ccordeiro72@gmail.com", "rasmenezes@gmail.com"]
    yag = yagmail.SMTP(email_user, email_pass)
    yag.send(to=destinatarios, subject=assunto, contents=corpo)


def main() -> None:
    logging.info("Iniciando...")
    state = load_state()

    logging.info(f"Buscando HTML: {URL_SITE}")
    raw_html = fetch_html(URL_SITE)

    pdf_urls = extrair_links_pdf_do_html(raw_html)
    if not pdf_urls:
        # Loga um pedacinho pra diagnóstico (não é sensível: página pública)
        snippet = normalize_html_for_pdf_search(raw_html)[:800]
        logging.error("Snippet do HTML (normalizado) para debug:\n" + snippet)
        raise RuntimeError("Não encontrei URLs de PDF no HTML do /servicos/diario.")

    pdf_url = escolher_pdf_mais_recente(pdf_urls)
    logging.info(f"PDF mais recente escolhido: {pdf_url}")

    last_link = state.get("last_link")
    if SEND_ONLY_IF_NEW and last_link == pdf_url:
        logging.info("Mesmo link do último run. Encerrando sem e-mail.")
        return

    logging.info("Baixando PDF...")
    pdf_bytes = baixar_pdf(pdf_url, NOME_ARQUIVO)
    pdf_hash = sha256_bytes(pdf_bytes)
    logging.info(f"PDF baixado. SHA256={pdf_hash}")

    last_hash = state.get("last_sha256")
    if SEND_ONLY_IF_NEW and last_hash == pdf_hash:
        logging.info("Mesmo conteúdo (SHA) do último run. Encerrando sem e-mail.")
        state["last_link"] = pdf_url
        state["last_sha256"] = pdf_hash
        save_state(state)
        return

    logging.info("Extraindo texto do PDF...")
    texto_pdf = ler_texto_pdf(NOME_ARQUIVO)

    trecho = extrair_entre(texto_pdf, "Nomear", "Procurador-Geral de Justiça")
    achou_concurso = contem_concurso_publico(texto_pdf)

    tem_match = bool(trecho) or achou_concurso
    if SEND_ONLY_IF_MATCH and not tem_match:
        logging.info("Sem match e SEND_ONLY_IF_MATCH=true. Não envia e-mail.")
        state["last_link"] = pdf_url
        state["last_sha256"] = pdf_hash
        save_state(state)
        return

    assunto = "E o MP?"

    corpo = []
    if trecho:
        corpo.append("Texto encontrado:\n\n")
        corpo.append(trecho)
        corpo.append("\n\n")
    else:
        corpo.append("NAAAAAAADA.\n\n")

    if achou_concurso:
        corpo.append("Observação: O termo 'concurso público' foi encontrado no documento.\n")

    corpo.append(f"\nAcesse o PDF aqui: {pdf_url}\n")
    conteudo_email = "".join(corpo)

    logging.info("Enviando e-mail...")
    enviar_email(assunto, conteudo_email)
    logging.info("E-mail enviado com sucesso!")

    state["last_link"] = pdf_url
    state["last_sha256"] = pdf_hash
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Falha na execução: {e}")
        sys.exit(1)
