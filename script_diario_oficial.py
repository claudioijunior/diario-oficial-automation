import logging
import os
import re
import sys
import json
import hashlib
import shutil
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import requests
import pdfplumber
import yagmail

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service

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


def baixar_pdf(url: str, destino: str) -> Tuple[bytes, str]:
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

    return r.content, content_type


def ler_texto_pdf(destino: str) -> str:
    partes = []
    with pdfplumber.open(destino) as pdf:
        for p in pdf.pages:
            partes.append(p.extract_text() or "")
    return "\n".join(partes)


def extrair_entre(texto: str, inicio: str, fim: str) -> Optional[str]:
    padrao = re.compile(
        re.escape(inicio) + r".*?" + re.escape(fim),
        flags=re.IGNORECASE | re.DOTALL
    )
    m = padrao.search(texto)
    return m.group(0) if m else None


def contem_concurso_publico(texto: str) -> bool:
    return re.search(r"concurso público", texto, flags=re.IGNORECASE) is not None


def extrair_edicao(texto_pdf: str, link_pdf: str) -> Optional[str]:
    # 1) tenta pelo link (ex.: ...-n-870-...)
    m = re.search(r"-n-(\d+)-", link_pdf, flags=re.IGNORECASE)
    if m:
        return m.group(1)

    # 2) tenta pelo texto (ex.: "Edição 848")
    m = re.search(r"\bEdi[cç][aã]o\s+(\d+)\b", texto_pdf, flags=re.IGNORECASE)
    if m:
        return m.group(1)

    # 3) tenta pelo "Nº 848"
    m = re.search(r"\bN[ºo]\s+(\d+)\b", texto_pdf, flags=re.IGNORECASE)
    if m:
        return m.group(1)

    return None


def _run_version(cmd: List[str]) -> str:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        return out
    except Exception:
        return "(não foi possível obter versão)"


def parse_timestamp_from_href(href: str) -> Optional[datetime]:
    # padrão comum: ...-YYYY-MM-DD-HH-MM-SS.pdf
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.pdf", href)
    if not m:
        return None
    y, mo, d, hh, mm, ss = map(int, m.groups())
    return datetime(y, mo, d, hh, mm, ss)


def escolher_link_mais_recente(hrefs: List[str]) -> str:
    candidatos: List[Tuple[Optional[datetime], str]] = []
    for h in hrefs:
        candidatos.append((parse_timestamp_from_href(h), h))

    # Ordena: timestamp conhecido primeiro, depois desempata por href
    candidatos.sort(key=lambda x: (x[0] is None, x[0] or datetime.min, x[1]))
    # pega o “máximo” pelo timestamp, mas a lista está crescente → pega o último com timestamp
    com_ts = [c for c in candidatos if c[0] is not None]
    if com_ts:
        return com_ts[-1][1]

    # fallback: se não achou timestamp, usa o último da lista (comportamento antigo)
    return hrefs[-1]


def enviar_email(assunto: str, corpo: str) -> None:
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASS")
    if not email_user or not email_pass:
        raise RuntimeError("EMAIL_USER/EMAIL_PASS não definidos (secrets).")

    destinatarios = ["ccordeiro72@gmail.com", "rasmenezes@gmail.com"]
    yag = yagmail.SMTP(email_user, email_pass)
    yag.send(to=destinatarios, subject=assunto, contents=corpo)


def main() -> None:
    logging.info("Iniciando o script...")
    state = load_state()

    # --- Selenium setup ---
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    chrome_bin = os.getenv("CHROME_BIN") or os.getenv("CHROME_PATH")
    chromedriver_bin = os.getenv("CHROMEDRIVER_BIN")

    if not chrome_bin:
        chrome_bin = shutil.which("google-chrome") or shutil.which("chrome")
    if not chromedriver_bin:
        chromedriver_bin = shutil.which("chromedriver")

    if not chrome_bin:
        raise RuntimeError("Chrome não encontrado. Verifique CHROME_BIN no workflow.")
    if not chromedriver_bin:
        raise RuntimeError("ChromeDriver não encontrado. Verifique CHROMEDRIVER_BIN no workflow.")

    options.binary_location = chrome_bin
    service = Service(chromedriver_bin)

    logging.info(f"Usando Chrome: {chrome_bin} | {_run_version([chrome_bin, '--version'])}")
    logging.info(f"Usando ChromeDriver: {chromedriver_bin} | {_run_version([chromedriver_bin, '--version'])}")

    driver = webdriver.Chrome(service=service, options=options)
    logging.info("WebDriver inicializado.")

    link_pdf_completo = None

    try:
        logging.info(f"Acessando site: {URL_SITE}")
        driver.get(URL_SITE)

        elementos = WebDriverWait(driver, 25).until(
            EC.presence_of_all_elements_located((By.XPATH, "//a[contains(@class, 'fc-day-grid-event')]"))
        )

        hrefs = []
        for el in elementos:
            href = el.get_attribute("href")
            if href:
                hrefs.append(href)

        if not hrefs:
            raise RuntimeError("Nenhum link de diário encontrado na página (hrefs vazios).")

        link_pdf = escolher_link_mais_recente(hrefs)
        link_pdf_completo = f"https://www.mprr.mp.br{link_pdf}" if link_pdf.startswith("/") else link_pdf

        logging.info(f"PDF escolhido: {link_pdf_completo}")

        last_link = state.get("last_link")
        if SEND_ONLY_IF_NEW and last_link == link_pdf_completo:
            logging.info("PDF já processado anteriormente (mesmo link). Encerrando sem enviar e-mail.")
            return

        logging.info("Baixando PDF...")
        pdf_bytes, content_type = baixar_pdf(link_pdf_completo, NOME_ARQUIVO)
        pdf_hash = sha256_bytes(pdf_bytes)
        logging.info(f"PDF baixado. SHA256={pdf_hash} | Content-Type={content_type}")

        last_hash = state.get("last_sha256")
        if SEND_ONLY_IF_NEW and last_hash == pdf_hash:
            logging.info("Conteúdo do PDF igual ao último processado (mesmo SHA). Encerrando sem enviar e-mail.")
            state["last_link"] = link_pdf_completo
            state["last_sha256"] = pdf_hash
            save_state(state)
            return

        logging.info("Extraindo texto do PDF...")
        texto_pdf = ler_texto_pdf(NOME_ARQUIVO)

        trecho = extrair_entre(texto_pdf, "Nomear", "Procurador-Geral de Justiça")
        achou_concurso = contem_concurso_publico(texto_pdf)
        edicao = extrair_edicao(texto_pdf, link_pdf_completo)

        # Decide se vale enviar
        tem_match = bool(trecho) or achou_concurso
        if SEND_ONLY_IF_MATCH and not tem_match:
            logging.info("Sem correspondências (match) e SEND_ONLY_IF_MATCH=true. Não envia e-mail.")
            state["last_link"] = link_pdf_completo
            state["last_sha256"] = pdf_hash
            save_state(state)
            return

        assunto = "E o MP?"
        if edicao:
            assunto += f" (Edição {edicao})"

        corpo = []
        if trecho:
            corpo.append("Texto encontrado:\n\n")
            corpo.append(trecho)
            corpo.append("\n\n")
        else:
            corpo.append("NAAAAAAADA.\n\n")

        if achou_concurso:
            corpo.append("Observação: O termo 'concurso público' foi encontrado no documento.\n")

        corpo.append(f"\nAcesse o PDF aqui: {link_pdf_completo}\n")

        conteudo_email = "".join(corpo)

        logging.info("Enviando e-mail...")
        enviar_email(assunto, conteudo_email)
        logging.info("E-mail enviado com sucesso!")

        # Atualiza state
        state["last_link"] = link_pdf_completo
        state["last_sha256"] = pdf_hash
        save_state(state)

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        logging.info("Navegador fechado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Falha na execução: {e}")
        sys.exit(1)
