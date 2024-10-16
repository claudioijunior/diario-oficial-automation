from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import requests
import pdfplumber
import re
import yagmail
import os
import logging

# Configurar logging para acompanhar a execução
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info("Iniciando o script...")

# Definir as opções do Chrome para rodar sem interface gráfica
options = webdriver.ChromeOptions()
options.add_argument("--headless")  # Executar no modo headless (sem interface gráfica)
options.add_argument("--no-sandbox")  # Necessário para alguns ambientes CI
options.add_argument("--disable-dev-shm-usage")  # Previne problemas de memória compartilhada
options.add_argument("--remote-debugging-port=9222")  # Debug remoto para evitar crashes

# Inicializa o ChromeDriver com as opções definidas
driver = webdriver.Chrome(options=options)
logging.info("ChromeDriver inicializado.")

try:
    # Acessar o site do Diário Oficial
    url_site = 'https://www.mprr.mp.br/servicos/diario'
    logging.info(f"Acessando site: {url_site}")
    driver.get(url_site)

    # Aguardar até que os elementos de download estejam visíveis (até 15 segundos)
    elementos_download = WebDriverWait(driver, 15).until(
        EC.presence_of_all_elements_located((By.XPATH, "//a[contains(@class, 'fc-day-grid-event')]"))
    )
    
    logging.info("Links de download encontrados.")
    
    # Pega o link do último elemento (mais recente)
    link_pdf = elementos_download[-1].get_attribute('href')
    link_pdf_completo = f"https://www.mprr.mp.br{link_pdf}" if link_pdf.startswith("/") else link_pdf
    logging.info(f"Link do PDF mais recente encontrado: {link_pdf_completo}")

    # Baixar o PDF
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        response = requests.get(link_pdf_completo, headers=headers)
        response.raise_for_status()  # Levanta exceção para erros HTTP
        if "text/html" in response.headers.get("Content-Type", ""):
            logging.error("Recebido HTML em vez de PDF.")
            driver.quit()
            exit(1)
        elif response.status_code == 200:
            nome_arquivo = 'diario_oficial_mais_recente.pdf'
            with open(nome_arquivo, 'wb') as file:
                file.write(response.content)
            logging.info(f"PDF baixado com sucesso: {nome_arquivo}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro ao fazer o download do PDF: {e}")
        driver.quit()
        exit(1)
    
    # Extrair texto entre "Nomear" e "Procurador-Geral de Justiça"
    def extrair_texto_completo(nome_arquivo, palavra_inicio, palavra_fim):
        with pdfplumber.open(nome_arquivo) as pdf:
            for pagina in pdf.pages:
                texto = pagina.extract_text()
                padrao = rf'{palavra_inicio}\s*.*?\s*{palavra_fim}'
                match = re.search(padrao, texto, re.DOTALL | re.IGNORECASE)
                if match:
                    return match.group(0)
        return None

    # Verificar se "concurso público" aparece no PDF de forma case-insensitive
    def verificar_concurso_publico(nome_arquivo):
        with pdfplumber.open(nome_arquivo) as pdf:
            for pagina in pdf.pages:
                texto = pagina.extract_text()
                if re.search(r'concurso público', texto, re.IGNORECASE):  # Pesquisa case-insensitive
                    return True
    return False

    # Extrair o texto relevante e verificar "concurso público"
    texto_extraido = extrair_texto_completo(nome_arquivo, "Nomear", "Procurador-Geral de Justiça")
    encontrou_concurso_publico = verificar_concurso_publico(nome_arquivo)

    # Conteúdo do e-mail
    conteudo_email = ""
    if texto_extraido:
        conteudo_email += f"Texto encontrado:\n{texto_extraido}\n\n"
    else:
        conteudo_email += "Nenhuma ocorrência encontrada.\n\n"

    if encontrou_concurso_publico:
        conteudo_email += "Observação: O termo 'concurso público' foi encontrado no documento.\n"

    # Adicionar o link para o PDF se houver ocorrências
    if texto_extraido or encontrou_concurso_publico:
        conteudo_email += f"\nAcesse o PDF aqui: {link_pdf_completo}"

    # Configurar e enviar o e-mail
    email_user = os.getenv('EMAIL_USER')
    email_pass = os.getenv('EMAIL_PASS')

    try:
        yag = yagmail.SMTP(email_user, email_pass)
        yag.send(to='ccordeiro72@gmail.com', subject='Resultado Diário - Diário Oficial', contents=conteudo_email)
        logging.info("E-mail enviado com sucesso!")
    except Exception as e:
        logging.error(f"Erro ao enviar e-mail: {e}")

finally:
    # Fechar o navegador
    driver.quit()
    logging.info("Navegador fechado.")
