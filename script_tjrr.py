import logging
import os
import re
import sys
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import unicodedata

import requests
import pdfplumber
import yagmail

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_PDF_URL = "https://diario.tjrr.jus.br/dpj/dpj-{date}.pdf"
NOME_ARQUIVO = "diario_tjrr_mais_recente.pdf"

STATE_DIR = ".state"
STATE_FILE = os.path.join(STATE_DIR, "last_seen_tjrr.json")

SEND_ONLY_IF_NEW = os.getenv("SEND_ONLY_IF_NEW", "true").strip().lower() in {"1", "true", "yes", "y"}
SEND_ONLY_IF_MATCH = os.getenv("SEND_ONLY_IF_MATCH", "false").strip().lower() in {"1", "true", "yes", "y"}

TERM_ALVO = "vii concurso publico"


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


def normalize_text(s: str) -> str:
    s = s.lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_business_day(d: datetime) -> bool:
    return d.weekday() < 5  # 0=segunda, 4=sexta


def candidate_pdf_urls(days_back: int = 15) -> List[str]:
    """
    Gera URLs candidatas, da data de hoje voltando alguns dias,
    considerando apenas dias úteis.
    """
    urls = []
    today = datetime.now()
    for i in range(days_back + 1):
        d = today - timedelta(days=i)
        if is_business_day(d):
            urls.append(BASE_PDF_URL.format(date=d.strftime("%Y%m%d")))
    return urls


def baixar_pdf(url: str, destino: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=90, allow_redirects=True)

    if r.status_code != 200:
        raise RuntimeError(f"Status HTTP inesperado ao baixar PDF: {r.status_code}")

    content_type = (r.headers.get("Content-Type") or "").lower()
    if "pdf" not in content_type and not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"Conteúdo não parece PDF. Content-Type={content_type}")

    if not r.content.startswith(b"%PDF"):
        raise RuntimeError("Conteúdo baixado não parece PDF (header %PDF não encontrado).")

    with open(destino, "wb") as f:
        f.write(r.content)

    return r.content


def encontrar_pdf_mais_recente() -> str:
    """
    Tenta encontrar o diário mais recente testando as URLs esperadas dos últimos dias úteis.
    """
    headers = {"User-Agent": "Mozilla/5.0"}

    for url in candidate_pdf_urls(days_back=20):
        try:
            logging.info(f"Testando PDF: {url}")
            r = requests.get(url, headers=headers, timeout=30, allow_redirects=True, stream=True)
            if r.status_code != 200:
                logging.info(f"Não encontrado ({r.status_code}): {url}")
                continue

            content_type = (r.headers.get("Content-Type") or "").lower()
            if "pdf" in content_type:
                logging.info(f"PDF mais recente encontrado: {url}")
                r.close()
                return url

            # fallback: às vezes o header pode vir estranho, então lê um pedaço
            chunk = next(r.iter_content(chunk_size=8), b"")
            r.close()
            if chunk.startswith(b"%PDF"):
                logging.info(f"PDF mais recente encontrado via header binário: {url}")
                return url

            logging.info(f"Arquivo não aparenta ser PDF: {url} | content-type={content_type}")
        except Exception as e:
            logging.warning(f"Falha ao testar {url}: {e}")

    raise RuntimeError("Não foi possível localizar o PDF mais recente do DJE/TJRR.")


def ler_texto_pdf(destino: str) -> str:
    partes = []
    with pdfplumber.open(destino) as pdf:
        for p in pdf.pages:
            partes.append(p.extract_text() or "")
    return "\n".join(partes)


def split_portarias(texto: str) -> List[str]:
    """
    Tenta separar blocos iniciados por PORTARIA.
    """
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")

    # divide antes de cada ocorrência relevante de PORTARIA
    partes = re.split(r"(?=^\s*PORTARIA\b)", texto, flags=re.IGNORECASE | re.MULTILINE)
    blocos = []

    for parte in partes:
        trecho = parte.strip()
        if not trecho:
            continue
        if re.match(r"^\s*PORTARIA\b", trecho, flags=re.IGNORECASE):
            blocos.append(trecho)

    return blocos


def extrair_portarias_com_termo(texto: str, termo: str) -> List[str]:
    termo_norm = normalize_text(termo)
    portarias = split_portarias(texto)

    encontrados = []
    for bloco in portarias:
        if termo_norm in normalize_text(bloco):
            encontrados.append(bloco)

    if encontrados:
        return encontrados

    # fallback: se não encontrou por split de portaria, devolve trechos contextuais
    return extrair_trechos_contextuais(texto, termo)


def extrair_trechos_contextuais(texto: str, termo: str, contexto_chars: int = 1800) -> List[str]:
    """
    Fallback caso o formato do PDF não permita separar bem por PORTARIA.
    Retorna trechos em volta da ocorrência do termo.
    """
    texto_norm = normalize_text(texto)
    termo_norm = normalize_text(termo)

    resultados = []
    start = 0
    while True:
        idx = texto_norm.find(termo_norm, start)
        if idx == -1:
            break

        ini = max(0, idx - contexto_chars)
        fim = min(len(texto), idx + len(termo) + contexto_chars)
        trecho = texto[ini:fim].strip()
        resultados.append(trecho)

        start = idx + len(termo_norm)

    # remove duplicados mantendo ordem
    unicos = []
    vistos = set()
    for r in resultados:
        chave = normalize_text(r[:500])
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(r)

    return unicos


def resumir_bloco(bloco: str, limite: int = 5000) -> str:
    bloco = bloco.strip()
    if len(bloco) <= limite:
        return bloco
    return bloco[:limite] + "\n\n[trecho truncado]"


def enviar_email(assunto: str, corpo: str) -> None:
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASS")
    if not email_user or not email_pass:
        raise RuntimeError("EMAIL_USER/EMAIL_PASS não definidos.")

    destinatarios = ["ccordeiro72@gmail.com"]
    yag = yagmail.SMTP(email_user, email_pass)
    yag.send(to=destinatarios, subject=assunto, contents=corpo)


def main() -> None:
    logging.info("Iniciando automação do DJE/TJRR...")
    state = load_state()

    pdf_url = encontrar_pdf_mais_recente()

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
        logging.info("Mesmo conteúdo do último run. Encerrando sem e-mail.")
        state["last_link"] = pdf_url
        state["last_sha256"] = pdf_hash
        save_state(state)
        return

    logging.info("Extraindo texto do PDF...")
    texto_pdf = ler_texto_pdf(NOME_ARQUIVO)

    logging.info("Procurando portarias com o termo alvo...")
    portarias_encontradas = extrair_portarias_com_termo(texto_pdf, TERM_ALVO)

    if SEND_ONLY_IF_MATCH and not portarias_encontradas:
        logging.info("Nenhuma portaria com o termo alvo encontrada e SEND_ONLY_IF_MATCH=true. Não envia e-mail.")
        state["last_link"] = pdf_url
        state["last_sha256"] = pdf_hash
        save_state(state)
        return

    corpo = []

    if portarias_encontradas:
        corpo.append(f"Foram encontradas {len(portarias_encontradas)} ocorrência(s) relacionadas a '{TERM_ALVO}'.\n\n")
        for i, portaria in enumerate(portarias_encontradas, start=1):
            corpo.append(f"===== TRECHO {i} =====\n")
            corpo.append(resumir_bloco(portaria))
            corpo.append("\n\n")
        corpo.append(f"PDF: {pdf_url}\n")
    else:
        corpo.append("NAAAAAAADA.\n\n")
        corpo.append(f"PDF verificado: {pdf_url}\n")

    conteudo_email = "".join(corpo)

    logging.info("Enviando e-mail...")
    enviar_email("E o TJRR?", conteudo_email)
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
