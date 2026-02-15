import os
import re
import sys
import logging
from datetime import datetime
from typing import Optional, List, Dict

import requests
import pdfplumber
import io

URL_SITE = "https://www.mprr.mp.br/servicos/diario"

MAX_YEARS = int(os.getenv("MAX_YEARS", "20"))                   # limite de anos a varrer
MAX_PDFS_PER_YEAR = int(os.getenv("MAX_PDFS_PER_YEAR", "120"))  # limite de PDFs por ano

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ---------- HTML helpers ----------
def normalize_html_for_pdf_search(raw: str) -> str:
    # URLs podem vir escapadas dentro de JS/JSON
    s = raw
    s = s.replace("\\u002F", "/").replace("\\u002f", "/")
    s = s.replace("\\/", "/")
    return s


def extract_csrf_token(html: str) -> str:
    m = re.search(r'name="_token"\s+type="hidden"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError("CSRF _token não encontrado no HTML.")
    return m.group(1)


def extract_available_years(html: str) -> List[int]:
    # Pega anos a partir do select id="ano"
    years = set()
    for m in re.finditer(r'<option\s+value="(\d{4})"', html):
        years.add(int(m.group(1)))
    return sorted(years, reverse=True)


def fetch_year_page(session: requests.Session, year: int) -> str:
    """
    Faz GET pra pegar _token e cookie, depois POST com 'ano'.
    """
    r1 = session.get(URL_SITE, timeout=60)
    r1.raise_for_status()
    token = extract_csrf_token(r1.text)

    r2 = session.post(URL_SITE, data={"_token": token, "ano": str(year)}, timeout=60)
    r2.raise_for_status()

    if f"Mostrando o ano: {year}" not in r2.text:
        logging.warning(f"Página não confirmou 'Mostrando o ano: {year}'. Continuando mesmo assim.")

    return r2.text


# ---------- PDF link extraction ----------
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


def extract_pdf_urls_from_html(raw_html: str) -> List[str]:
    html_norm = normalize_html_for_pdf_search(raw_html)
    urls = set()

    # relativo
    for m in re.finditer(r"(/servicos/download/[^\"'<>\s]+?\.pdf)", html_norm, flags=re.IGNORECASE):
        urls.add("https://www.mprr.mp.br" + m.group(1))

    # absoluto
    for m in re.finditer(
        r"(https?://www\.mprr\.mp\.br/servicos/download/[^\"'<>\s]+?\.pdf)",
        html_norm,
        flags=re.IGNORECASE,
    ):
        urls.add(m.group(1))

    # filtra para diários (caso venha pdf de outra coisa)
    filtered = [u for u in urls if "diario" in u.lower() and "mprr" in u.lower()]
    return sorted(filtered if filtered else list(urls))


def sort_pdfs_desc(urls: List[str]) -> List[str]:
    def key(u: str):
        ts = parse_timestamp_from_url(u)
        ed = parse_edicao_from_url(u)
        return (
            ts is None,
            ts or datetime.min,
            ed is None,
            ed or -1,
            u,
        )

    # mais recente primeiro
    return list(reversed(sorted(urls, key=key)))


# ---------- PDF scanning ----------
def download_pdf(session: requests.Session, url: str) -> bytes:
    r = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=90, allow_redirects=True)
    r.raise_for_status()

    ct = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ct:
        raise RuntimeError("Recebido HTML em vez de PDF.")

    if not r.content.startswith(b"%PDF"):
        raise RuntimeError("Conteúdo não parece PDF (%PDF não encontrado).")

    return r.content


def extract_between(text: str, start: str, end: str) -> Optional[str]:
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), flags=re.IGNORECASE | re.DOTALL)
    m = pat.search(text)
    return m.group(0) if m else None


def extract_names_from_nomeacao_block(block: str) -> List[str]:
    """
    Heurística simples: tenta capturar sequências em MAIÚSCULAS típicas de nomes.
    Não é perfeito — por isso devolve lista.
    """
    s = re.sub(r"\s+", " ", block)

    candidates = re.findall(
        r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ]{2,}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ]{2,}){1,6}\b",
        s
    )

    stop = {
        "NOMEAR", "PROCURADOR", "GERAL", "JUSTIÇA", "MINISTÉRIO", "PÚBLICO",
        "ESTADO", "RORAIMA", "CONCURSO", "PÚBLICO", "IV"
    }

    out = []
    for c in candidates:
        if c in stop:
            continue
        if sum(1 for w in c.split() if w in stop) >= 2:
            continue
        out.append(c)

    seen = set()
    dedup = []
    for n in out:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    return dedup


def pdf_has_relevant_nomeacao(pdf_bytes: bytes) -> Optional[Dict[str, str]]:
    """
    Critério do usuário:
    - conter "Nomear"
    - conter "IV Concurso Público"
    """
    alvo = "iv concurso público"

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            low = text.lower()

            if "nomear" not in low:
                continue
            if alvo not in low:
                continue

            snippet = extract_between(text, "Nomear", "Procurador-Geral de Justiça") or text
            names = extract_names_from_nomeacao_block(snippet)

            return {
                "page": str(page.page_number),
                "snippet": snippet.strip(),
                "names": ", ".join(names) if names else "(não consegui extrair nomes com segurança)",
            }

    return None


# ---------- main ----------
def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    logging.info(f"Acessando {URL_SITE} para descobrir anos disponíveis...")
    r = session.get(URL_SITE, timeout=60)
    r.raise_for_status()

    years = extract_available_years(r.text)
    if not years:
        raise RuntimeError("Não consegui extrair anos disponíveis do select#ano.")

    years = years[:MAX_YEARS]
    logging.info(f"Anos a varrer (desc): {years}")

    for year in years:
        logging.info(f"Carregando ano {year} via POST...")
        html_year = fetch_year_page(session, year)

        pdf_urls = sort_pdfs_desc(extract_pdf_urls_from_html(html_year))
        logging.info(f"Ano {year}: {len(pdf_urls)} PDFs encontrados.")

        for i, pdf_url in enumerate(pdf_urls[:MAX_PDFS_PER_YEAR], start=1):
            logging.info(f"[{year}] PDF {i}/{min(len(pdf_urls), MAX_PDFS_PER_YEAR)}: {pdf_url}")

            try:
                pdf_bytes = download_pdf(session, pdf_url)
                found = pdf_has_relevant_nomeacao(pdf_bytes)
                if found:
                    print("\n=== ÚLTIMA NOMEAÇÃO ENCONTRADA (critério: 'Nomear' + 'IV Concurso Público') ===")
                    print(f"Ano: {year}")
                    print(f"PDF: {pdf_url}")
                    print(f"Página: {found['page']}")
                    print(f"Nomes (heurística): {found['names']}")
                    print("\nTrecho:")
                    print(found["snippet"])
                    return

            except Exception as e:
                logging.warning(f"Falha processando PDF: {e}")
                continue

    print("Não encontrei nomeação compatível nos anos/PDFs varridos.")
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Erro fatal: {e}")
        sys.exit(1)
