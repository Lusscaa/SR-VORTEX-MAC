"""CNPJ extraction engine.

Reads a Receita Federal CNPJ card (PDF or image) and returns a validated
``CNPJData`` object. Tries ``pdfplumber`` first for native PDFs; falls back
to ``pytesseract`` when the PDF has no extractable text or the input is an
image.

O modelo ``CNPJData`` mantém campos planos em ``snake_case`` por compatibilidade
com o restante do código Python (``AutomationRunner`` preenche os formulários
do Meta usando esses campos diretamente). Para emitir o payload no formato
exigido pela Cloudflare Worker, use ``to_worker_dict()`` — ele traduz
camelCase + estrutura aninhada ``endereco``/``contato``.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import IO, Any, Optional, Union

import pdfplumber
import pytesseract
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from config import TESSERACT_CMD, TESSERACT_LANG
from utils.helpers import (
    clean_text,
    format_cnpj,
    is_valid_cnpj,
    only_digits,
    strip_accents,
)

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


FileInput = Union[str, Path, bytes, IO[bytes]]

_NI = "Não informado"


class CNPJData(BaseModel):
    # --- Identificação ------------------------------------------------------
    razao_social: Optional[str] = Field(default=None)
    nome_fantasia: Optional[str] = Field(default=None)
    cnpj: Optional[str] = Field(default=None)

    # --- Enquadramento (alinhado ao contrato da Worker) ---------------------
    abertura: Optional[str] = Field(default=None)
    situacao: Optional[str] = Field(default=None)
    natureza_juridica: Optional[str] = Field(default=None)
    porte: Optional[str] = Field(default=None)
    mei: Optional[str] = Field(default=None)
    capital_social: Optional[str] = Field(default=None)
    cnae_principal: Optional[str] = Field(default=None)
    atividades_secundarias: list[str] = Field(default_factory=list)

    # --- Campos do Comprovante de Inscrição (Cartão CNPJ) -------------------
    # Capturados da BrasilAPI para renderizar o comprovante oficial localmente
    # (core/cnpj_reva.render_comprovante_local). Só usados no template.
    matriz_filial: Optional[str] = Field(default=None)        # "MATRIZ"/"FILIAL"
    data_situacao: Optional[str] = Field(default=None)        # data_situacao_cadastral
    motivo_situacao: Optional[str] = Field(default=None)      # desc_motivo_situacao
    efr: Optional[str] = Field(default=None)                  # ente federativo resp.
    situacao_especial: Optional[str] = Field(default=None)
    data_situacao_especial: Optional[str] = Field(default=None)

    # --- Endereço (mantido flat por compat com automation.py) ---------------
    logradouro: Optional[str] = Field(default=None)
    numero: Optional[str] = Field(default=None)
    complemento: Optional[str] = Field(default=None)
    bairro: Optional[str] = Field(default=None)
    cidade: Optional[str] = Field(default=None)
    uf: Optional[str] = Field(default=None)
    cep: Optional[str] = Field(default=None)

    # --- Contato ------------------------------------------------------------
    telefone: Optional[str] = Field(default=None)
    email: Optional[str] = Field(default=None)

    raw_text: str = Field(default="", repr=False)

    @field_validator("cnpj")
    @classmethod
    def _format_cnpj(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        return format_cnpj(v)

    @field_validator("uf")
    @classmethod
    def _upper_uf(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v

    @field_validator("mei")
    @classmethod
    def _normalize_mei(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        norm = v.strip().lower()
        if norm.startswith("sim") or norm.startswith("s"):
            return "Sim"
        if norm.startswith("nao") or norm.startswith("não") or norm.startswith("n"):
            return "Não"
        return v

    @property
    def endereco_completo(self) -> str:
        parts = [
            self.logradouro,
            self.numero,
            self.bairro,
            self.cidade,
            self.uf,
            self.cep,
        ]
        return ", ".join(p for p in parts if p)

    @property
    def cnpj_valido(self) -> bool:
        return bool(self.cnpj) and is_valid_cnpj(self.cnpj)

    # ------------------------------------------------------------------
    # Contrato JSON da Cloudflare Worker
    # ------------------------------------------------------------------
    def to_worker_dict(self) -> dict[str, Any]:
        """Emite o payload no schema exato consumido pelo ``worker.js``.

        Aplica os mesmos defaults ("Não informado", "S/N", "") usados pelo
        ``fallbackEmpresa`` da Worker, para que a saída fique idempotente
        entre OCR local e os fallbacks de API (BrasilAPI / CNPJa / ReceitaWS).
        """
        return {
            "cnpj": self.cnpj or _NI,
            "razaoSocial": self.razao_social or _NI,
            "nomeFantasia": self.nome_fantasia or _NI,
            "abertura": self.abertura or _NI,
            "situacao": self.situacao or _NI,
            "naturezaJuridica": self.natureza_juridica or _NI,
            "porte": self.porte or _NI,
            "mei": self.mei or _NI,
            "capitalSocial": self.capital_social or _NI,
            "cnaePrincipal": self.cnae_principal or _NI,
            "atividadesSecundarias": list(self.atividades_secundarias or []),
            "endereco": {
                "logradouro": self.logradouro or _NI,
                "numero": self.numero or "S/N",
                "complemento": self.complemento or "",
                "bairro": self.bairro or _NI,
                "cidade": self.cidade or _NI,
                "uf": self.uf or _NI,
                "cep": self.cep or _NI,
            },
            "contato": {
                "telefone": self.telefone or _NI,
                "email": self.email or _NI,
            },
        }


# ---------------------------------------------------------------------------
# Regex patterns — tolerant to OCR noise (extra spaces, accent loss).
# We match against the accent-stripped, single-line uppercased version.
# ---------------------------------------------------------------------------
_PATTERNS = {
    "cnpj": re.compile(r"\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})\b"),
    "cep": re.compile(r"\b(\d{5}-?\d{3})\b"),
    "telefone": re.compile(
        r"(?:TELEFONE|TEL|FONE|DDD\s*\d|DDD\s+TELEFONE)[^\d(+]{0,15}"
        r"(\+?\d{0,3}[\s.()-]*\d{2,4}[\s.()-]*\d{3,5}[\s.()-]*\d{3,4})"
    ),
    "email": re.compile(
        r"(?:E-?MAIL|ENDERECO ELETRONICO|EMAIL)\s*[:\-]?\s*"
        r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})"
    ),
    "razao_social": re.compile(
        r"NOME EMPRESARIAL\s*[:\-]?\s*(.+?)"
        r"(?=\s+(?:TITULO DO ESTABELECIMENTO|NOME FANTASIA|"
        r"PORTE|DATA DE ABERTURA|CODIGO|NATUREZA|CAPITAL|$))",
    ),
    "nome_fantasia": re.compile(
        r"(?:TITULO DO ESTABELECIMENTO\s*\(?\s*NOME DE FANTASIA\s*\)?|NOME FANTASIA)"
        r"\s*[:\-]?\s*(.+?)"
        r"(?=\s+(?:PORTE|CODIGO|DATA|ATIVIDADE|NATUREZA|$))",
    ),
    "logradouro": re.compile(
        r"LOGRADOURO\s*[:\-]?\s*(.+?)"
        r"(?=\s+(?:NUMERO|COMPLEMENTO|CEP|BAIRRO|$))",
    ),
    "numero": re.compile(
        r"\bNUMERO\s*[:\-]?\s*([A-Z0-9./-]+?)"
        r"(?=\s+(?:COMPLEMENTO|CEP|BAIRRO|MUNICIPIO|$))",
    ),
    "complemento": re.compile(
        r"\bCOMPLEMENTO\s*[:\-]?\s*(.+?)"
        r"(?=\s+(?:CEP|BAIRRO|MUNICIPIO|UF|ENDERECO|$))",
    ),
    "bairro": re.compile(
        r"BAIRRO\s*/?\s*DISTRITO\s*[:\-]?\s*(.+?)"
        r"(?=\s+(?:CEP|MUNICIPIO|UF|ENDERECO|$))",
    ),
    "cidade": re.compile(
        r"MUNICIPIO\s*[:\-]?\s*(.+?)"
        r"(?=\s+(?:UF|ESTADO|CEP|$))",
    ),
    "uf": re.compile(r"\bUF\s*[:\-]?\s*([A-Z]{2})\b"),

    # --- Enquadramento ----------------------------------------------------
    "abertura": re.compile(
        r"DATA DE ABERTURA\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})"
    ),
    "situacao": re.compile(
        r"SITUACAO CADASTRAL\s*[:\-]?\s*([A-Z\s]+?)"
        r"(?=\s+(?:DATA DA SITUACAO|MOTIVO|$))"
    ),
    "natureza_juridica": re.compile(
        r"(?:CODIGO E DESCRICAO DA NATUREZA JURIDICA|NATUREZA JURIDICA)"
        r"\s*[:\-]?\s*(?:\d{3}-?\d?\s*-\s*)?(.+?)"
        r"(?=\s+(?:LOGRADOURO|ENDERECO|NUMERO|CAPITAL|PORTE|$))"
    ),
    "porte": re.compile(
        r"\bPORTE\s*[:\-]?\s*([A-Z\s]+?)"
        r"(?=\s+(?:CODIGO|DATA|NATUREZA|OPCAO|ATIVIDADE|$))"
    ),
    "mei": re.compile(
        r"OPCAO PELO\s+(?:SIMPLES|MEI)\s*[:\-]?\s*(SIM|NAO)"
    ),
    "capital_social": re.compile(
        r"CAPITAL SOCIAL\s*[:\-]?\s*(R?\$?\s*[\d\.,]+)"
    ),
    "cnae_principal": re.compile(
        r"CODIGO E DESCRICAO DA ATIVIDADE ECONOMICA PRINCIPAL"
        r"\s*[:\-]?\s*(\d{2}\.\d{2}-\d-\d{2}\s*-\s*.+?)"
        r"(?=\s+(?:CODIGO E DESCRICAO DAS ATIVIDADES|NATUREZA|LOGRADOURO|$))"
    ),
}

# Bloco onde ficam as secundárias — parseado em segundo passe para extrair
# múltiplos itens CNAE "XX.XX-X-XX - DESCRICAO".
_SECUNDARIAS_BLOCK = re.compile(
    r"CODIGO E DESCRICAO DAS ATIVIDADES ECONOMICAS SECUNDARIAS"
    r"\s*[:\-]?\s*(.+?)"
    r"(?=\s+(?:CODIGO E DESCRICAO DA NATUREZA|NATUREZA JURIDICA|LOGRADOURO|ENDERECO ELETRONICO|TELEFONE|$))"
)
_CNAE_ITEM = re.compile(
    r"(\d{2}\.\d{2}-\d-\d{2})\s*-\s*(.+?)(?=\s+\d{2}\.\d{2}-\d-\d{2}\s*-|$)"
)


class CNPJExtractor:
    """Extract structured CNPJ data from a PDF or image."""

    def __init__(self, ocr_lang: str = TESSERACT_LANG) -> None:
        self.ocr_lang = ocr_lang

    # ----- public API ------------------------------------------------------
    def extract(self, source: FileInput, *, filename: Optional[str] = None) -> CNPJData:
        data, name = self._load_bytes(source, filename)
        suffix = Path(name).suffix.lower() if name else ""

        if suffix == ".pdf" or self._looks_like_pdf(data):
            text = self._extract_pdf_text(data)
            if not text.strip():
                text = self._ocr_pdf_pages(data)
        else:
            text = self._ocr_image(data)

        return self._parse(text)

    # ----- loading ---------------------------------------------------------
    @staticmethod
    def _load_bytes(source: FileInput, filename: Optional[str]) -> tuple[bytes, str]:
        if isinstance(source, (str, Path)):
            path = Path(source)
            return path.read_bytes(), path.name
        if isinstance(source, bytes):
            return source, filename or ""
        # file-like
        data = source.read()
        name = filename or getattr(source, "name", "") or ""
        return data, name

    @staticmethod
    def _looks_like_pdf(data: bytes) -> bool:
        return data[:5] == b"%PDF-"

    # ----- extraction ------------------------------------------------------
    @staticmethod
    def _extract_pdf_text(data: bytes) -> str:
        chunks: list[str] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
        return "\n".join(chunks)

    def _ocr_pdf_pages(self, data: bytes) -> str:
        """Render PDF pages with pdfplumber and OCR each rendered image."""
        chunks: list[str] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pil = page.to_image(resolution=300).original
                chunks.append(pytesseract.image_to_string(pil, lang=self.ocr_lang))
        return "\n".join(chunks)

    def _ocr_image(self, data: bytes) -> str:
        image = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(image, lang=self.ocr_lang)

    # ----- parsing ---------------------------------------------------------
    def _parse(self, raw: str) -> CNPJData:
        cleaned = clean_text(raw)
        haystack = strip_accents(cleaned).upper()
        single_line = re.sub(r"\s+", " ", haystack)

        fields: dict[str, Any] = {}
        for key, pattern in _PATTERNS.items():
            match = pattern.search(single_line)
            fields[key] = self._postprocess(key, match.group(1)) if match else None

        # CEP precisa de formatação especial após capturar.
        if fields.get("cep"):
            fields["cep"] = self._format_cep(fields["cep"])

        # Atividades secundárias — parser separado (lista de strings).
        fields["atividades_secundarias"] = self._parse_secundarias(single_line)

        return CNPJData(raw_text=cleaned, **fields)

    @staticmethod
    def _parse_secundarias(single_line: str) -> list[str]:
        block_match = _SECUNDARIAS_BLOCK.search(single_line)
        if not block_match:
            return []
        block = block_match.group(1)
        items: list[str] = []
        for m in _CNAE_ITEM.finditer(block):
            code = m.group(1).strip()
            desc = re.sub(r"\s{2,}", " ", m.group(2)).strip(" .;,-")
            if desc:
                items.append(f"{code} - {desc}")
        return items

    @staticmethod
    def _postprocess(key: str, value: str) -> str:
        value = value.strip(" :-\t")
        if key == "cnpj":
            return format_cnpj(value)
        if key == "telefone":
            digits = only_digits(value)
            if not digits:
                return value
            if len(digits) >= 11:
                return f"({digits[-11:-9]}) {digits[-9:-4]}-{digits[-4:]}"
            if len(digits) == 10:
                return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
            return digits
        if key == "email":
            # Voltamos para o caso minúsculo (regex roda em UPPER).
            return value.lower()
        if key == "capital_social":
            # Garante prefixo "R$ ".
            v = re.sub(r"\s+", " ", value).strip()
            if not v.upper().startswith("R$"):
                v = f"R$ {v.lstrip('$').strip()}"
            return v
        if key == "situacao":
            # "ATIVA" → "Ativa" para combinar com o exemplo do contrato.
            v = re.sub(r"\s{2,}", " ", value).strip().lower().capitalize()
            return v
        return re.sub(r"\s{2,}", " ", value).strip()

    @staticmethod
    def _format_cep(value: str) -> str:
        digits = only_digits(value)
        if len(digits) != 8:
            return value
        return f"{digits[:5]}-{digits[5:]}"
