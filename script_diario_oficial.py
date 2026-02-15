import logging
import os
import re
import sys
import json
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List
import html as htmlmod
import unicodedata

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

# termo alvo dentro do trecho de nomeação
TERM_IV = "iv concurso publico"


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


def normalize_html_for_pdf_search(raw: str) -> str:
    s = htmlmod.unescape(raw)
    s = s.replace("\\u002F", "/").replace("\\u002f", "/")
    s = s.replace("\\/", "/")
    return s


def normalize_text(s: str) -> str:
    # lower + remove acentos
    s = s.lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s


def parse_timestamp_from_url(u: str) -> Optional[datetime]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.pdf", u)
    if not m:
        return None
    y, mo, d, hh, mm, ss = map(int, m.groups())
    return datetime(y, mo, d, hh, mm, ss)


def parse_edicao_from_url(u: str) -> Optional[int]:
    m = re.search(r"-n-(\d+)-", u, flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def extrair_links_pdf_do_html(raw_html: str) -> List[str]:
    html_norm = normalize_html_for_pdf_search(raw_html)
    urls = set()

    for m in re.finditer(r"(/servicos/download/[^\"'<>\s]+?\.pdf)", html_norm, flags=re.IGNORECASE):
        urls.add("https://www.mprr.mp.br" + m.group(1))

    for m in re.finditer(
        r"(https?://www\.mprr\.mp\.br/servicos/download/[^\"'<>\s]+?\.pdf)",
        html_norm,
        flags=re.IGNORECASE,
    ):
        urls.add(m.group(1))

    if not urls:
        for m in re.finditer(r"([^\s\"'<>]+?\.pdf)", html_norm, flags=re.IGNORECASE):
            candidate = m.group(1)
            if "mprr.mp.br" in candidate:
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                elif candidate.startswith("/"):
                    candidate = "https://www.mprr.mp.br" + candidate
                urls.add(candidate)

    filtradas = [u for u in urls if "diario" in u.lower() and "mprr" in u.lower()]
    final = filtradas if filtradas else list(urls)

    final_sorted = sorted(final)
    logging.info(f"Encontrados {len(final_sorted)} PDFs (após normalização).")
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


def extrair_trecho_nomear_pgj(texto: str) -> Optional[str]:
    """
    Extrai trecho entre "Nomear" e "Procurador-Geral de Justiça" de forma mais flexível
    (tolerando espaços e 'Justiça/Justica').
    """
    padrao = re.compile(
        r"Nomear\s*.*?\s*Procurador\s*-\s*Geral\s+de\s+Justi[çc]a",
        flags=re.IGNORECASE | re.DOTALL,
    )
    m = padrao.search(texto)
    return m.group(0) if m else None


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

    trecho_nomeacao = extrair_trecho_nomear_pgj(texto_pdf)

    # >>> regra nova: só conta se "IV Concurso Público" estiver DENTRO do trecho Nomear..PGJ
    nomeacao_iv_encontrada = False
    trecho_relevante = None
    if trecho_nomeacao:
        if TERM_IV in normalize_text(trecho_nomeacao):
            nomeacao_iv_encontrada = True
            trecho_relevante = trecho_nomeacao

    # Quando SEND_ONLY_IF_MATCH=true, só envia se a nomeação do IV concurso foi encontrada
    if SEND_ONLY_IF_MATCH and not nomeacao_iv_encontrada:
        logging.info("Sem nomeação do IV Concurso Público no trecho Nomear..PGJ e SEND_ONLY_IF_MATCH=true. Não envia e-mail.")
        state["last_link"] = pdf_url
        state["last_sha256"] = pdf_hash
        save_state(state)
        return

    # Corpo do e-mail
    corpo = []
    if nomeacao_iv_encontrada and trecho_relevante:
        corpo.append("Nomeação encontrada (IV Concurso Público):\n\n")
        corpo.append(trecho_relevante)
        corpo.append("\n\n")
        corpo.append(f"Acesse o PDF aqui: {pdf_url}\n")
    else:
        corpo.append("NAAAAAAADA.\n\n")

    conteudo_email = "".join(corpo)

    logging.info("Enviando e-mail...")
    enviar_email("E o MP?", conteudo_email)
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
