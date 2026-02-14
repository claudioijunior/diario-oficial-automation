import logging
import os
import re
import sys
import requests
import pdfplumber
import yagmail

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info("Iniciando o script...")

URL_SITE = "https://www.mprr.mp.br/servicos/diario"
NOME_ARQUIVO = "diario_oficial_mais_recente.pdf"

def baixar_pdf(url: str, destino: str) -> None:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()

    # Algumas vezes servidor manda HTML (erro/redirect)
    content_type = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        raise RuntimeError("Recebido HTML em vez de PDF (Content-Type text/html).")

    # Validação simples de “cara de PDF”
    if not r.content.startswith(b"%PDF"):
        # Ainda pode ser PDF válido sem header no primeiro chunk? raro. Aqui é bom falhar pra não dar falso positivo.
        raise RuntimeError("Conteúdo baixado não parece PDF (header %PDF não encontrado).")

    with open(destino, "wb") as f:
        f.write(r.content)

def ler_texto_pdf(destino: str) -> str:
    partes = []
    with pdfplumber.open(destino) as pdf:
        for p in pdf.pages:
            texto = p.extract_text() or ""
            partes.append(texto)
    return "\n".join(partes)

def extrair_entre(texto: str, inicio: str, fim: str) -> str | None:
    # Escapa termos pra evitar surpresas de regex
    padrao = re.compile(
        re.escape(inicio) + r".*?" + re.escape(fim),
        flags=re.IGNORECASE | re.DOTALL
    )
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

# --- Selenium setup ---
options = webdriver.ChromeOptions()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

# Se você instalou o chromedriver em /usr/local/bin/chromedriver, forçe:
service = Service("/usr/local/bin/chromedriver")

driver = webdriver.Chrome(service=service, options=options)
logging.info("ChromeDriver inicializado.")

try:
    logging.info(f"Acessando site: {URL_SITE}")
    driver.get(URL_SITE)

    elementos = WebDriverWait(driver, 20).until(
        EC.presence_of_all_elements_located((By.XPATH, "//a[contains(@class, 'fc-day-grid-event')]"))
    )
    logging.info("Links encontrados.")

    link_pdf = elementos[-1].get_attribute("href")
    link_pdf_completo = f"https://www.mprr.mp.br{link_pdf}" if link_pdf.startswith("/") else link_pdf
    logging.info(f"PDF mais recente: {link_pdf_completo}")

    logging.info("Baixando PDF...")
    baixar_pdf(link_pdf_completo, NOME_ARQUIVO)
    logging.info(f"PDF salvo em: {NOME_ARQUIVO}")

    logging.info("Extraindo texto do PDF...")
    texto_pdf = ler_texto_pdf(NOME_ARQUIVO)

    trecho = extrair_entre(texto_pdf, "Nomear", "Procurador-Geral de Justiça")
    achou_concurso = contem_concurso_publico(texto_pdf)

    corpo = []
    if trecho:
        corpo.append("Texto encontrado:\n")
        corpo.append(trecho)
        corpo.append("\n")
    else:
        corpo.append("NAAAAAAADA.\n")

    if achou_concurso:
        corpo.append("\nObservação: O termo 'concurso público' foi encontrado no documento.\n")

    if trecho or achou_concurso:
        corpo.append(f"\nAcesse o PDF aqui: {link_pdf_completo}")

    conteudo_email = "".join(corpo)

    logging.info("Enviando e-mail...")
    enviar_email("E o MP?", conteudo_email)
    logging.info("E-mail enviado com sucesso!")

except Exception as e:
    logging.error(f"Falha na execução: {e}")
    sys.exit(1)

finally:
    try:
        driver.quit()
    except Exception:
        pass
    logging.info("Navegador fechado.")
