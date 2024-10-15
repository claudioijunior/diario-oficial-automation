from selenium import webdriver
from selenium.webdriver.common.by import By
import time
import requests
import pdfplumber
import re
import yagmail
import os

# Definir as opções do Chrome para rodar sem interface gráfica
options = webdriver.ChromeOptions()
options.add_argument("--headless")  # Executar no modo headless (sem interface gráfica)
options.add_argument("--no-sandbox")  # Necessário para alguns ambientes CI
options.add_argument("--disable-dev-shm-usage")  # Previne problemas de memória compartilhada
options.add_argument("--remote-debugging-port=9222")  # Debug remoto para evitar crashes

# Inicializa o ChromeDriver com as opções definidas
driver = webdriver.Chrome(options=options)

# Acessar o site do Diário Oficial
url_site = 'https://www.mprr.mp.br/servicos/diario'
driver.get(url_site)

# Espera 10 segundos para garantir o carregamento
time.sleep(10)

# Localizar os links de download
elementos_download = driver.find_elements(By.XPATH, "//a[contains(@class, 'fc-day-grid-event')]")

if elementos_download:
    link_pdf = elementos_download[-1].get_attribute('href')
    link_pdf_completo = f"https://www.mprr.mp.br{link_pdf}" if link_pdf.startswith("/") else link_pdf

    print(f"Link do PDF mais recente encontrado: {link_pdf_completo}")

    # Baixar o PDF
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(link_pdf_completo, headers=headers)

    if "text/html" in response.headers.get("Content-Type", ""):
        print("Recebido HTML em vez de PDF.")
    elif response.status_code == 200:
        nome_arquivo = 'diario_oficial_mais_recente.pdf'
        with open(nome_arquivo, 'wb') as file:
            file.write(response.content)
        print(f"PDF baixado com sucesso: {nome_arquivo}")
    else:
        print(f"Erro ao baixar o PDF. Status: {response.status_code}")
else:
    print("Nenhum botão de download encontrado.")

# Extrair texto entre "Conceder" e "Procurador-Geral de Justiça"
def extrair_texto_completo(nome_arquivo, palavra_inicio, palavra_fim):
    with pdfplumber.open(nome_arquivo) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text()
            padrao = rf'{palavra_inicio}.*?{palavra_fim}'
            match = re.search(padrao, texto, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(0)
    return None

texto_extraido = extrair_texto_completo(nome_arquivo, "Conceder", "Procurador-Geral de Justiça")

# Conteúdo do e-mail
conteudo_email = f"Texto encontrado:\n{texto_extraido}" if texto_extraido else "Nenhuma ocorrência encontrada."

# Configurar e enviar o e-mail
email_user = os.getenv('EMAIL_USER')
email_pass = os.getenv('EMAIL_PASS')

try:
    yag = yagmail.SMTP(email_user, email_pass)
    yag.send(to='ccordeiro72@gmail.com', subject='Resultado Diário - Diário Oficial', contents=conteudo_email)
    print("E-mail enviado com sucesso!")
except Exception as e:
    print(f"Erro ao enviar e-mail: {e}")

# Fechar o navegador
driver.quit()
