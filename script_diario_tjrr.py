import logging
import os
import re
import sys
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import unicodedata

import requests
import pdfplumber
import yagmail

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_PDF_URL = "https://diario.tjrr.jus.br/dpj/dpj-{date}.pdf"
DOWNLOAD_DIR = "downloads_tjrr"

STATE_DIR = ".state"
STATE_FILE = os.path.join(STATE_DIR, "last_seen_tjrr.json")

SEND_ONLY_IF_NEW = os.getenv("SEND_ONLY_IF_NEW", "true").strip().lower() in {"1", "true", "yes", "y"}

TERM_ALVO = "VII Concurso Público"


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
    return d.weekday() < 5


def pdf_url_for_date(d: datetime) -> str:
    return BASE_PDF_URL.format(date=d.strftime("%Y%m%d"))


def week_range_from_date(d: datetime) -> Tuple[datetime, datetime]:
    monday = d - timedelta(days=d.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def get_today() -> datetime:
    return datetime.now()


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

    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "wb") as f:
        f.write(r.content)

    return r.content


def pdf_exists(url: str) -> bool:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True, stream=True)
        if r.status_code != 200:
            r.close()
            return False

        content_type = (r.headers.get("Content-Type") or "").lower()
        if "pdf" in content_type:
            r.close()
            return True

        chunk = next(r.iter_content(chunk_size=8), b"")
        r.close()
        return chunk.startswith(b"%PDF")
    except Exception as e:
        logging.warning(f"Falha ao verificar existência do PDF {url}: {e}")
        return False


def ler_texto_pdf(destino: str) -> str:
    partes = []
    with pdfplumber.open(destino) as pdf:
        for p in pdf.pages:
            partes.append(p.extract_text() or "")
    return "\n".join(partes)


def split_blocos_relevantes(texto: str) -> List[str]:
    """
    Tenta separar blocos como PORTARIA, EDITAL, ATO, EXTRATO etc.
    Isso dá mais robustez, porque o termo pode aparecer em vários tipos de ato.
    """
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")

    marcadores = [
        r"PORTARIA",
        r"EDITAL",
        r"ATO",
        r"EXTRATO",
        r"RESOLUÇÃO",
        r"RESOLUCAO",
        r"AVISO",
    ]
    pattern = r"(?=^\s*(?:" + "|".join(marcadores) + r")\b)"
    partes = re.split(pattern, texto, flags=re.IGNORECASE | re.MULTILINE)

    blocos = []
    for parte in partes:
        trecho = parte.strip()
        if not trecho:
            continue
        if re.match(r"^\s*(PORTARIA|EDITAL|ATO|EXTRATO|RESOLUÇÃO|RESOLUCAO|AVISO)\b", trecho, flags=re.IGNORECASE):
            blocos.append(trecho)

    return blocos


def extrair_trechos_contextuais(texto: str, termo: str, contexto_chars: int = 1800) -> List[str]:
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

    unicos = []
    vistos = set()
    for r in resultados:
        chave = normalize_text(r[:500])
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(r)

    return unicos


def extrair_ocorrencias(texto: str, termo: str) -> List[str]:
    termo_norm = normalize_text(termo)
    blocos = split_blocos_relevantes(texto)

    encontrados = []
    for bloco in blocos:
        if termo_norm in normalize_text(bloco):
            encontrados.append(bloco)

    if encontrados:
        return encontrados

    return extrair_trechos_contextuais(texto, termo)


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

    destinatarios = ["ccordeiro72@gmail.com", "rasmenezes@gmail.com"]
    yag = yagmail.SMTP(email_user, email_pass)
    yag.send(to=destinatarios, subject=assunto, contents=corpo)


def processar_diario(data_ref: datetime) -> Dict[str, Any]:
    """
    Processa um único diário e retorna metadados + ocorrências.
    """
    url = pdf_url_for_date(data_ref)
    nome_arquivo = os.path.join(DOWNLOAD_DIR, f"dpj-{data_ref.strftime('%Y%m%d')}.pdf")

    resultado = {
        "data": data_ref,
        "url": url,
        "exists": False,
        "sha256": None,
        "occurrences": [],
        "error": None,
    }

    if not pdf_exists(url):
        return resultado

    resultado["exists"] = True

    try:
        pdf_bytes = baixar_pdf(url, nome_arquivo)
        resultado["sha256"] = sha256_bytes(pdf_bytes)

        texto_pdf = ler_texto_pdf(nome_arquivo)
        resultado["occurrences"] = extrair_ocorrencias(texto_pdf, TERM_ALVO)
        return resultado
    except Exception as e:
        resultado["error"] = str(e)
        return resultado


def datas_uteis_da_semana(data_base: datetime) -> List[datetime]:
    monday, friday = week_range_from_date(data_base)
    datas = []
    atual = monday
    while atual <= friday:
        if is_business_day(atual):
            datas.append(atual)
        atual += timedelta(days=1)
    return datas


def montar_email_semanal(resultados: List[Dict[str, Any]], monday: datetime, friday: datetime) -> Tuple[str, str]:
    ocorrencias_totais = []
    diarios_processados = []

    for r in resultados:
        if r["exists"]:
            diarios_processados.append(r["data"].strftime("%d/%m/%Y"))
        for occ in r["occurrences"]:
            ocorrencias_totais.append((r["data"], r["url"], occ))

    corpo = []

    if ocorrencias_totais:
        assunto = "Monitoramento do Diário do TJRR - ocorrências identificadas na semana"
        corpo.append(
            f'Foram identificadas {len(ocorrencias_totais)} ocorrência(s) relacionadas ao termo "{TERM_ALVO}" '
            f'no período de {monday.strftime("%d/%m/%Y")} a {friday.strftime("%d/%m/%Y")}.\n\n'
        )

        for i, (data_ref, url, occ) in enumerate(ocorrencias_totais, start=1):
            corpo.append(f"===== OCORRÊNCIA {i} =====\n")
            corpo.append(f"Data do diário: {data_ref.strftime('%d/%m/%Y')}\n")
            corpo.append(f"PDF analisado: {url}\n\n")
            corpo.append(resumir_bloco(occ))
            corpo.append("\n\n")
    else:
        assunto = "Monitoramento do Diário do TJRR - resumo semanal sem ocorrências"
        corpo.append(
            f'Informamos que, no período de {monday.strftime("%d/%m/%Y")} a {friday.strftime("%d/%m/%Y")}, '
            f'não foram identificadas ocorrências relacionadas ao termo "{TERM_ALVO}" nos diários monitorados.\n\n'
        )

    if diarios_processados:
        corpo.append("Diários efetivamente analisados nesta semana:\n")
        for d in diarios_processados:
            corpo.append(f"- {d}\n")
        corpo.append("\n")

    erros = [r for r in resultados if r.get("error")]
    if erros:
        corpo.append("Ocorreram falhas em alguns processamentos:\n")
        for r in erros:
            corpo.append(f"- {r['data'].strftime('%d/%m/%Y')}: {r['error']}\n")
        corpo.append("\n")

    return assunto, "".join(corpo)


def montar_email_diario(resultado: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """
    Segunda a quinta: só envia se houver ocorrência no diário do dia.
    """
    ocorrencias = resultado["occurrences"]
    if not ocorrencias:
        return None

    assunto = "Monitoramento do Diário do TJRR - ocorrência identificada"

    corpo = []
    corpo.append(
        f'Foram identificadas {len(ocorrencias)} ocorrência(s) relacionadas ao termo "{TERM_ALVO}" '
        f'no diário de {resultado["data"].strftime("%d/%m/%Y")}.\n\n'
    )

    for i, occ in enumerate(ocorrencias, start=1):
        corpo.append(f"===== OCORRÊNCIA {i} =====\n")
        corpo.append(resumir_bloco(occ))
        corpo.append("\n\n")

    corpo.append(f"PDF analisado: {resultado['url']}\n")
    return assunto, "".join(corpo)


def main() -> None:
    logging.info("Iniciando automação robusta do DJE/TJRR...")
    state = load_state()

    hoje = get_today()
    weekday = hoje.weekday()  # 0=segunda ... 4=sexta

    # Sexta: consolida a semana inteira
    if weekday == 4:
        monday, friday = week_range_from_date(hoje)
        resultados = []

        hash_semana = hashlib.sha256()
        diarios_existentes = []

        for data_ref in datas_uteis_da_semana(hoje):
            logging.info(f"Processando diário semanal: {data_ref.strftime('%Y-%m-%d')}")
            r = processar_diario(data_ref)
            resultados.append(r)

            if r["exists"] and r["sha256"]:
                diarios_existentes.append(r["url"])
                hash_semana.update(r["sha256"].encode("utf-8"))

        assinatura_semana = hash_semana.hexdigest()
        chave_semana = f"{monday.strftime('%Y%m%d')}_{friday.strftime('%Y%m%d')}"

        if SEND_ONLY_IF_NEW:
            last_week_key = state.get("last_week_key")
            last_week_hash = state.get("last_week_hash")
            if last_week_key == chave_semana and last_week_hash == assinatura_semana:
                logging.info("Resumo semanal já enviado anteriormente para esta mesma semana. Encerrando.")
                return

        assunto, corpo = montar_email_semanal(resultados, monday, friday)
        enviar_email(assunto, corpo)

        state["last_week_key"] = chave_semana
        state["last_week_hash"] = assinatura_semana
        state["last_week_urls"] = diarios_existentes
        save_state(state)
        logging.info("E-mail semanal enviado com sucesso.")
        return

    # Segunda a quinta: verifica apenas o diário do dia
    if weekday < 4:
        logging.info(f"Processando diário do dia: {hoje.strftime('%Y-%m-%d')}")
        resultado = processar_diario(hoje)

        if not resultado["exists"]:
            logging.info("Diário do dia ainda não disponível. Encerrando sem e-mail.")
            return

        if SEND_ONLY_IF_NEW:
            last_link = state.get("last_link")
            last_hash = state.get("last_sha256")

            if last_link == resultado["url"]:
                if last_hash == resultado["sha256"]:
                    logging.info("Mesmo PDF já processado anteriormente. Encerrando sem e-mail.")
                    return

        email_data = montar_email_diario(resultado)
        if email_data is None:
            logging.info("Sem ocorrências no diário do dia. Encerrando sem e-mail.")
            state["last_link"] = resultado["url"]
            state["last_sha256"] = resultado["sha256"]
            save_state(state)
            return

        assunto, corpo = email_data
        enviar_email(assunto, corpo)

        state["last_link"] = resultado["url"]
        state["last_sha256"] = resultado["sha256"]
        save_state(state)
        logging.info("E-mail diário enviado com sucesso.")
        return

    # Sábado e domingo
    logging.info("Hoje não é dia útil de execução relevante. Encerrando.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Falha na execução: {e}")
        sys.exit(1)
