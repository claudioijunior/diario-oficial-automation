from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
import time
import requests
import pdfplumber
import re
import yagmail
import os

# Caminho para o ChromeDriver
caminho_driver = 'C:/Users/Claudio/Documents/code/python/chromedriver-win64/chromedriver.exe'

# Inicializando o driver do Chrome
service = Service(executable_path=caminho_driver)
driver = webdriver.Chrome(service=service)

# Acessar o site do Diário Oficial
url_site = 'https://www.mprr.mp.br/servicos/diario'
driver.get(url_site)

# Espera 5 segundos para visualizar o navegador abrindo
time.sleep(10)

# Localizar todos os links de download no calendário usando a classe do <a>
# Usando XPath para localizar elementos com a classe 'fc-day-grid-event'
elementos_download = driver.find_elements(By.XPATH, "//a[contains(@class, 'fc-day-grid-event')]")

# Pega o link do último elemento (mais recente) encontrado
if elementos_download:
    link_pdf = elementos_download[-1].get_attribute('href')
    
    # Verificar se o link é relativo ou absoluto
    if link_pdf.startswith("/"):
        link_pdf_completo = f"https://www.mprr.mp.br{link_pdf}"
    else:
        link_pdf_completo = link_pdf

    print(f"Link do PDF mais recente encontrado: {link_pdf_completo}")

    # Fazendo o download do PDF usando requests com cabeçalho HTTP
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.111 Safari/537.36'
    }
    
    response = requests.get(link_pdf_completo, headers=headers)

    # Verificar se a resposta não é HTML (indicando uma página de erro ou redirecionamento)
    if "text/html" in response.headers.get("Content-Type", ""):
        print("Parece que a resposta é uma página HTML, não um PDF.")
        print(response.text)  # Mostra o conteúdo da resposta para debug
    else:
        # Verifica se o download foi bem-sucedido
        if response.status_code == 200:
            nome_arquivo = 'diario_oficial_mais_recente.pdf'
            with open(nome_arquivo, 'wb') as file:
                file.write(response.content)
            print(f"PDF baixado com sucesso: {nome_arquivo}")
        else:
            print(f"Erro ao baixar o PDF. Status Code: {response.status_code}")
else:
    print("Nenhum botão de download encontrado.")

# Função para verificar a palavra "Nomear" e extrair o texto até "Procurador-Geral de Justiça"
def extrair_texto_completo(nome_arquivo, palavra_inicio, palavra_fim):
    with pdfplumber.open(nome_arquivo) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text()
            # Procurar a palavra "Nomear" e capturar o conteúdo até "Procurador-Geral de Justiça"
            padrao = rf'{palavra_inicio}.*?{palavra_fim}'
            match = re.search(padrao, texto, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(0)  # Retorna o texto completo entre "Nomear" e "Procurador-Geral de Justiça"
    return None

# Verificando se a palavra "Nomear" está no PDF e extraindo o texto até "Procurador-Geral de Justiça"
palavra_inicio = "Conceder"
palavra_fim = "Procurador-Geral de Justiça"
texto_extraido = extrair_texto_completo(nome_arquivo, palavra_inicio, palavra_fim)

# Enviar o texto extraído por e-mail
if texto_extraido:
    conteudo_email = f"Texto encontrado:\n{texto_extraido}"
else:
    conteudo_email = "A palavra 'Nomear' NÃO foi encontrada no PDF."

# Recuperar as credenciais das variáveis de ambiente
email_user = os.getenv('EMAIL_USER')
email_pass = os.getenv('EMAIL_PASS')

# Configurar yagmail com seu e-mail e senha de aplicativo
yag = yagmail.SMTP(email_user, email_pass)

# Enviar o e-mail com o resultado
yag.send(
    to='ccordeiro72@gmail.com',  # Coloque o e-mail destinatário
    subject='Resultado Diário - Diário Oficial',
    contents=conteudo_email
)

print("E-mail enviado com sucesso!")

# Fecha o navegador
driver.quit()
