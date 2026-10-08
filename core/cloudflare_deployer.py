"""Cloudflare Workers deployer — pipeline de mutação + edge deploy.

Pipeline:
  1. ``render_worker(cnpj_digits, token_hash)``: localiza as duas constantes
     do template (``EMBEDDED_DEFAULT_CNPJ`` e
     ``EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION``) e substitui APENAS o literal
     entre aspas. O restante do JavaScript (incluindo template literals
     ``${body}``, ``${header()}``, ``${escapeHtml(code)}``) é preservado
     textualmente porque vive em uma raw-string Python.
  2. ``deploy(domain, cnpj_digits, token_hash)``: chama ``render_worker`` e
     faz ``PUT`` multipart no endpoint Workers da Cloudflare:
         PUT /accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}
     com partes ``metadata`` (JSON) + ``worker.js`` (ES module).

Sucesso = ``HTTP 200`` + ``response.json()["success"] == True``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

from pathlib import Path

from config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_ACCOUNT_IDS,
    CLOUDFLARE_API_BASE,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_API_TOKENS,
    CLOUDFLARE_COMPATIBILITY_DATE,
    CLOUDFLARE_REQUEST_TIMEOUT_S,
    CLOUDFLARE_ROTATION_STATE_PATH,
    CLOUDFLARE_ROTATION_THRESHOLD,
    CLOUDFLARE_SCRIPT_NAME,
)
from utils.logging_utils import install_redaction
from utils.retry import http_retry

logger = logging.getLogger("meta_automation.cloudflare_deployer")
install_redaction(logger)  # L3 — mascara Bearer/api_token/etc em logs

# ---------------------------------------------------------------------------
# Especificação multipart do endpoint Workers (ES modules).
# https://developers.cloudflare.com/api/operations/worker-script-upload-worker-module
#
# Contrato OBRIGATÓRIO para `export default { fetch }` (ES module):
#   * Parte "metadata" — Content-Type: application/json
#       Body: { "main_module": "<nome>", "compatibility_date": "YYYY-MM-DD" }
#       O campo `main_module` precisa bater EXATAMENTE com o nome de uma
#       das partes seguintes (case-sensitive).
#   * Parte "<MAIN_MODULE_NAME>" — Content-Type: application/javascript+module
#       Body: o JS do módulo (UTF-8).
#       ⚠ `application/javascript` SEM `+module` faz a CF interpretar como
#       Service Worker legacy → 400 Bad Request (metadata.main_module órfão).
# ---------------------------------------------------------------------------
MAIN_MODULE_NAME = "worker.js"
METADATA_CT = "application/json"
MODULE_CT = "application/javascript+module"

# ---------------------------------------------------------------------------
# Worker source — copiado textualmente da Worker oficial do cliente.
# Edite DIRETAMENTE este bloco quando o template canônico mudar.
# Raw string (r"""...""") preserva backticks, ${...}, regex literals e
# backslashes sem qualquer escape Python.
# ---------------------------------------------------------------------------
_WORKER_TEMPLATE = r"""
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const escapeHtml = (value) =>
      String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");

    const getFacebookDomainVerificationMeta = () => {
      const code =
        EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION ||
        (env && (env.FACEBOOK_DOMAIN_VERIFICATION || env.FACEBOOK_VERIFICATION)) ||
        "";
      if (!code) return "";
      return `<meta name="facebook-domain-verification" content="${escapeHtml(code)}" />`;
    };

    const normalizeCnpj = (value) => String(value || "").replace(/\D/g, "");
    const EMBEDDED_DEFAULT_CNPJ = "65144276000199";
    const EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION = "";

    const formatCnpj = (digits) => {
      const d = normalizeCnpj(digits);
      if (d.length !== 14) return digits;
      return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`;
    };

    const formatCep = (value) => {
      const d = String(value || "").replace(/\D/g, "");
      if (d.length !== 8) return value || "";
      return `${d.slice(0, 5)}-${d.slice(5)}`;
    };

    const formatDateBr = (iso) => {
      const value = String(iso || "");
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
      if (!match) return iso || "Não informado";
      return `${match[3]}/${match[2]}/${match[1]}`;
    };

    const formatCurrencyBr = (value) => {
      const numberValue = typeof value === "number" ? value : Number(String(value || "").replace(",", "."));
      if (!Number.isFinite(numberValue)) return value || "Não informado";
      return new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }).format(numberValue);
    };

    const formatTelefoneBr = (value) => {
      const d = String(value || "").replace(/\D/g, "");
      if (d.length === 10) return `(${d.slice(0, 2)}) ${d.slice(2, 6)}-${d.slice(6)}`;
      if (d.length === 11) return `(${d.slice(0, 2)}) ${d.slice(2, 7)}-${d.slice(7)}`;
      return value || "Não informado";
    };

    const toTelHref = (value) => {
      const d = String(value || "").replace(/\D/g, "");
      if (!d) return "";
      const e164 = d.startsWith("55") && d.length >= 12 ? `+${d}` : `+55${d}`;
      return `tel:${e164}`;
    };

    const formatCnaeId = (value) => {
      const d = String(value || "").replace(/\D/g, "");
      if (d.length !== 7) return "";
      return `${d.slice(0, 2)}.${d.slice(2, 4)}-${d.slice(4, 5)}-${d.slice(5)}`;
    };

    const fallbackEmpresa = {
      "cnpj": "65.144.276/0001-99",
      "razaoSocial": "65.144.276 MARIA APARECIDA DA SILVA",
      "nomeFantasia": "Não informado",
      "abertura": "15/02/2026",
      "situacao": "Ativa",
      "naturezaJuridica": "Empresário (Individual)",
      "porte": "Microempresa",
      "mei": "Sim",
      "capitalSocial": "R$ 2.000,00",
      "cnaePrincipal": "10.91-1-02 - Fabricação de produtos de padaria e confeitaria com predominância de produção própria",
      "atividadesSecundarias": [],
      "endereco": {
        "logradouro": "Rua Catao", "numero": "1094", "complemento": "", "bairro": "Vila Romana", "cidade": "São Paulo", "uf": "SP", "cep": "05049-000"
      },
      "contato": { "telefone": "(11) 9852-5285", "email": "mariaaparecida84259@gmail.com" }
    };

    const mapBrasilApiToEmpresa = (data) => {
      const logradouro = [data.descricao_tipo_de_logradouro, data.logradouro].filter(Boolean).join(" ").trim();
      const atividadesSecundarias = Array.isArray(data.cnaes_secundarios) ? data.cnaes_secundarios.map((item) => item && item.codigo && item.descricao ? `${item.codigo} - ${item.descricao}` : String(item || "")) : [];
      const telefoneRaw = data.ddd_telefone_1 || data.ddd_telefone_2 || "";
      return {
        cnpj: formatCnpj(data.cnpj),
        razaoSocial: data.razao_social || "Não informado",
        nomeFantasia: data.nome_fantasia || "Não informado",
        abertura: formatDateBr(data.data_inicio_atividade),
        situacao: data.descricao_situacao_cadastral || data.situacao_cadastral || "Não informado",
        naturezaJuridica: data.natureza_juridica || "Não informado",
        porte: data.descricao_porte || data.porte || "Não informado",
        mei: data.opcao_pelo_mei === true ? "Sim" : data.opcao_pelo_mei === false ? "Não" : "Não informado",
        capitalSocial: formatCurrencyBr(data.capital_social),
        cnaePrincipal: data.cnae_fiscal && data.cnae_fiscal_descricao ? `${data.cnae_fiscal} - ${data.cnae_fiscal_descricao}` : data.cnae_fiscal_descricao || "Não informado",
        atividadesSecundarias,
        endereco: {
          logradouro: logradouro || "Não informado", numero: data.numero || "S/N", complemento: data.complemento || "", bairro: data.bairro || "Não informado", cidade: data.municipio || "Não informado", uf: data.uf || "Não informado", cep: formatCep(data.cep),
        },
        contato: { telefone: formatTelefoneBr(telefoneRaw), email: data.email || "Não informado" },
      };
    };

    const mapCnpjaToEmpresa = (data) => {
      const address = data && data.address ? data.address : {};
      const company = data && data.company ? data.company : {};
      const status = data && data.status ? data.status : {};
      const telefone = Array.isArray(data?.phones) && data.phones.length ? formatTelefoneBr(`${data.phones[0].area || ""}${data.phones[0].number || ""}`) : "Não informado";
      const email = Array.isArray(data?.emails) && data.emails.length ? data.emails[0].address || "Não informado" : "Não informado";
      const cnaeCode = formatCnaeId(data?.mainActivity?.id);
      const cnaeText = data?.mainActivity?.text || "";
      const cnaePrincipal = cnaeCode && cnaeText ? `${cnaeCode} - ${cnaeText}` : cnaeText || "Não informado";
      const atividadesSecundarias = Array.isArray(data?.sideActivities) ? data.sideActivities.map((item) => { const code = formatCnaeId(item?.id); const text = item?.text || ""; if (code && text) return `${code} - ${text}`; return text || ""; }).filter(Boolean) : [];
      return {
        cnpj: formatCnpj(data?.taxId), razaoSocial: company?.name || "Não informado", nomeFantasia: data?.alias || "Não informado", abertura: formatDateBr(data?.founded), situacao: status?.text || "Não informado", naturezaJuridica: company?.nature?.text || "Não informado", porte: company?.size?.text || "Não informado",
        mei: company?.simei?.optant === true ? "Sim" : company?.simei?.optant === false ? "Não" : "Não informado", capitalSocial: formatCurrencyBr(company?.equity), cnaePrincipal, atividadesSecundarias,
        endereco: { logradouro: address?.street || "Não informado", numero: address?.number || "S/N", complemento: address?.details || "", bairro: address?.district || "Não informado", cidade: address?.city || "Não informado", uf: address?.state || "Não informado", cep: formatCep(address?.zip) },
        contato: { telefone, email },
      };
    };

    const htmlEntityDecode = (input) => {
      const text = String(input || "").replaceAll("&nbsp;", " ").replaceAll("&quot;", '"').replaceAll("&ldquo;", '"').replaceAll("&rdquo;", '"').replaceAll("&#039;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">");
      return text.replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => String.fromCodePoint(parseInt(hex, 16))).replace(/&#(\d+);/g, (_, dec) => String.fromCodePoint(parseInt(dec, 10)));
    };

    const stripHtmlToText = (html) => {
      const withoutScripts = String(html || "").replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ");
      const withNewlines = withoutScripts.replace(/<br\s*\/?>/gi, "\n").replace(/<\/p>/gi, "\n").replace(/<\/div>/gi, "\n").replace(/<\/li>/gi, "\n");
      const noTags = withNewlines.replace(/<[^>]+>/g, " ");
      return htmlEntityDecode(noTags).replace(/[ \t]+\n/g, "\n").trim();
    };

    const stateToUf = {
      acre: "AC", alagoas: "AL", amapa: "AP", "amapá": "AP", amazonas: "AM", bahia: "BA", ceara: "CE", "ceará": "CE", "distrito federal": "DF", "espirito santo": "ES", "espírito santo": "ES", goias: "GO", "goiás": "GO", maranhao: "MA", "maranhão": "MA", "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG", para: "PA", "pará": "PA", paraiba: "PB", "paraíba": "PB", parana: "PR", "paraná": "PR", pernambuco: "PE", piaui: "PI", "piauí": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN", "rio grande do sul": "RS", rondonia: "RO", "rondônia": "RO", roraima: "RR", "santa catarina": "SC", "sao paulo": "SP", "são paulo": "SP", sergipe: "SE", tocantins: "TO",
    };

    const mapCnpjBizTextToEmpresa = (text, cnpjDigits) => {
      const t = String(text || "");
      const capture = (re) => { const m = re.exec(t); return m ? m[1].trim() : ""; };
      const razaoSocial = capture(/Razão Social:\s*([\s\S]*?)\s*(Clique para copiar|Data da Abertura:)/i);
      const abertura = capture(/Data da Abertura:\s*(\d{2}\/\d{2}\/\d{4})/i);
      const porte = capture(/Porte:\s*([\s\S]*?)\s*Natureza Jurídica:/i);
      const naturezaJuridica = capture(/Natureza Jurídica:\s*([\s\S]*?)\s*(Clique para copiar|Opção pelo MEI:)/i);
      const mei = capture(/Opção pelo MEI:\s*(Sim|Não)/i);
      const situacao = capture(/Situação:\s*([\s\S]*?)\s*Data Situação Cadastral:/i);
      const capitalSocial = capture(/Capital Social:\s*(R\$\s*[\d\.\,]+)/i);
      const email = capture(/E-mail:\s*([\s\S]*?)\s*Clique para copiar/i);
      const telefoneMasked = capture(/Telefone\(s\):\s*([\s\S]*?)\s*Clique para copiar/i);
      const logradouroLine = capture(/Logradouro:\s*([\s\S]*?)\s*Bairro:/i);
      const bairro = capture(/Bairro:\s*([\s\S]*?)\s*CEP:/i);
      const cepRaw = capture(/CEP:\s*([0-9]{5}-?[0-9]{3})/i);
      const municipio = capture(/Município:\s*([\s\S]*?)\s*Estado:/i);
      const estado = capture(/Estado:\s*([\s\S]*?)\s*(Para correspondência:|Atividades|$)/i);

      let logradouro = logradouroLine; let numero = "S/N";
      const commaSplit = logradouroLine.split(",");
      if (commaSplit.length >= 2) {
        logradouro = commaSplit[0].trim(); numero = commaSplit.slice(1).join(",").trim() || "S/N";
      } else {
        const numMatch = /^(.*?)(\s+SN|\s+S\/N|\s+\d+[\w\-\/]*)$/i.exec(logradouroLine.trim());
        if (numMatch) { logradouro = numMatch[1].trim(); numero = numMatch[2].trim().replace(/^(\s+)/, "") || "S/N"; }
      }
      const uf = stateToUf[String(estado || "").toLowerCase().trim()] || (estado || "").slice(0, 2).toUpperCase();
      const principal = capture(/Principal:\s*([0-9.\-]+)\s*-\s*([^\n]+)/i); const cnaePrincipal = principal ? principal : "";
      const secundariasBlock = capture(/Secundária\(s\):\s*([\s\S]*?)\s*(Procurar no Google Maps|Atividades - CNAES|Sobre A empresa|$)/i);
      const atividadesSecundarias = secundariasBlock ? secundariasBlock.split("\n").map((line) => line.trim()).filter((line) => /^\d{2}\.\d{2}-\d-\d{2}\s*-/.test(line)) : [];

      return {
        cnpj: formatCnpj(cnpjDigits), razaoSocial: razaoSocial || "Não informado", nomeFantasia: "Não informado", abertura: abertura || "Não informado", situacao: situacao || "Não informado", naturezaJuridica: naturezaJuridica || "Não informado", porte: porte || "Não informado", mei: mei ? mei.charAt(0).toUpperCase() + mei.slice(1).toLowerCase() : "Não informado", capitalSocial: capitalSocial || "Não informado", cnaePrincipal: cnaePrincipal || "Não informado", atividadesSecundarias,
        endereco: { logradouro: logradouro || "Não informado", numero: numero || "S/N", complemento: "", bairro: bairro || "Não informado", cidade: municipio || "Não informado", uf: uf || "Não informado", cep: formatCep(cepRaw) },
        contato: { telefone: telefoneMasked || "Não informado", email: email || "Não informado" },
      };
    };

    const mapReceitaWsToEmpresa = (data) => {
      const principal = Array.isArray(data.atividade_principal) && data.atividade_principal.length ? data.atividade_principal[0] : null;
      const atividadesSecundarias = Array.isArray(data.atividades_secundarias) ? data.atividades_secundarias.map((item) => item && item.code && item.text ? `${item.code} - ${item.text}` : "").filter(Boolean) : [];
      return {
        cnpj: data.cnpj || formatCnpj(data.cnpj), razaoSocial: data.nome || "Não informado", nomeFantasia: data.fantasia || "Não informado", abertura: data.abertura || "Não informado", situacao: data.situacao || "Não informado", naturezaJuridica: data.natureza_juridica || "Não informado", porte: data.porte || "Não informado", mei: "Não informado", capitalSocial: formatCurrencyBr(data.capital_social),
        cnaePrincipal: principal ? `${principal.code} - ${principal.text}` : "Não informado", atividadesSecundarias,
        endereco: { logradouro: data.logradouro || "Não informado", numero: data.numero || "S/N", complemento: data.complemento || "", bairro: data.bairro || "Não informado", cidade: data.municipio || "Não informado", uf: data.uf || "Não informado", cep: formatCep(data.cep) },
        contato: { telefone: formatTelefoneBr(data.telefone), email: data.email || "Não informado" },
      };
    };

    const fetchEmpresaFromReceitaWs = async (cnpjDigits) => {
      const cacheKey = new Request(`https://cache.local/receitaws/${cnpjDigits}`);
      const cached = await caches.default.match(cacheKey); if (cached) return cached.json();
      const apiResponse = await fetch(`https://www.receitaws.com.br/v1/cnpj/${cnpjDigits}`, { headers: { accept: "application/json", "user-agent": "Mozilla/5.0" } });
      if (!apiResponse.ok) return null; const data = await apiResponse.json(); if (!data || data.status !== "OK") return null;
      const empresa = mapReceitaWsToEmpresa(data);
      if (ctx && typeof ctx.waitUntil === "function") {
        const responseToCache = new Response(JSON.stringify(empresa), { headers: { "content-type": "application/json; charset=UTF-8", "cache-control": "public, max-age=86400" } });
        ctx.waitUntil(caches.default.put(cacheKey, responseToCache));
      }
      return empresa;
    };

    const isEnderecoIncompleto = (empresaValue) => {
      if (!empresaValue || !empresaValue.endereco) return true; const e = empresaValue.endereco;
      return (!e.logradouro || e.logradouro === "Não informado" || !e.cidade || e.cidade === "Não informado" || !e.cep || e.cep === "Não informado");
    };

    const mergeEmpresa = (primary, secondary) => {
      if (!primary) return secondary; if (!secondary) return primary;
      const pick = (a, b) => (a && a !== "Não informado" ? a : b);
      return {
        cnpj: pick(primary.cnpj, secondary.cnpj), razaoSocial: pick(primary.razaoSocial, secondary.razaoSocial), nomeFantasia: pick(primary.nomeFantasia, secondary.nomeFantasia), abertura: pick(primary.abertura, secondary.abertura), situacao: pick(primary.situacao, secondary.situacao), naturezaJuridica: pick(primary.naturezaJuridica, secondary.naturezaJuridica), porte: pick(primary.porte, secondary.porte), mei: pick(primary.mei, secondary.mei), capitalSocial: pick(primary.capitalSocial, secondary.capitalSocial), cnaePrincipal: pick(primary.cnaePrincipal, secondary.cnaePrincipal),
        atividadesSecundarias: primary.atividadesSecundarias && primary.atividadesSecundarias.length ? primary.atividadesSecundarias : secondary.atividadesSecundarias || [],
        endereco: { logradouro: pick(primary.endereco?.logradouro, secondary.endereco?.logradouro), numero: pick(primary.endereco?.numero, secondary.endereco?.numero), complemento: pick(primary.endereco?.complemento, secondary.endereco?.complemento), bairro: pick(primary.endereco?.bairro, secondary.endereco?.bairro), cidade: pick(primary.endereco?.cidade, secondary.endereco?.cidade), uf: pick(primary.endereco?.uf, secondary.endereco?.uf), cep: pick(primary.endereco?.cep, secondary.endereco?.cep) },
        contato: { telefone: pick(primary.contato?.telefone, secondary.contato?.telefone), email: pick(primary.contato?.email, secondary.contato?.email) },
      };
    };

    const fetchEmpresaFromCnpjBiz = async (cnpjDigits) => {
      const cacheKey = new Request(`https://cache.local/cnpj-biz/${cnpjDigits}`);
      const cached = await caches.default.match(cacheKey); if (cached) return cached.json();
      const pageResponse = await fetch(`https://cnpj.biz/${cnpjDigits}`, { headers: { "user-agent": "Mozilla/5.0", accept: "text/html" } });
      if (!pageResponse.ok) return null; const html = await pageResponse.text(); const text = stripHtmlToText(html); const empresaBiz = mapCnpjBizTextToEmpresa(text, cnpjDigits);
      if (ctx && typeof ctx.waitUntil === "function") {
        const responseToCache = new Response(JSON.stringify(empresaBiz), { headers: { "content-type": "application/json; charset=UTF-8", "cache-control": "public, max-age=86400" } });
        ctx.waitUntil(caches.default.put(cacheKey, responseToCache));
      }
      return empresaBiz;
    };

    const fetchEmpresaFromCnpja = async (cnpjDigits) => {
      const cacheKey = new Request(`https://cache.local/cnpja/${cnpjDigits}`);
      const cached = await caches.default.match(cacheKey); if (cached) return cached.json();
      const apiResponse = await fetch(`https://open.cnpja.com/office/${cnpjDigits}`, { headers: { accept: "application/json", "user-agent": "Mozilla/5.0" } });
      if (!apiResponse.ok) return null; const data = await apiResponse.json(); const empresa = mapCnpjaToEmpresa(data);
      if (ctx && typeof ctx.waitUntil === "function") {
        const responseToCache = new Response(JSON.stringify(empresa), { headers: { "content-type": "application/json; charset=UTF-8", "cache-control": "public, max-age=86400" } });
        ctx.waitUntil(caches.default.put(cacheKey, responseToCache));
      }
      return empresa;
    };

    const fetchEmpresaByCnpj = async (cnpjValue) => {
      const cnpjDigits = normalizeCnpj(cnpjValue); if (cnpjDigits.length !== 14) return null;
      const empresaCnpja = await fetchEmpresaFromCnpja(cnpjDigits); if (empresaCnpja && !isEnderecoIncompleto(empresaCnpja)) return empresaCnpja;
      const cacheKey = new Request(`https://cache.local/cnpj/${cnpjDigits}`);
      const cached = await caches.default.match(cacheKey);
      if (cached) { const data = await cached.json(); const empresaBrasilApiCached = mapBrasilApiToEmpresa(data); return mergeEmpresa(empresaBrasilApiCached, empresaCnpja); }

      const apiUrl = `https://brasilapi.com.br/api/cnpj/v1/${cnpjDigits}`;
      const apiResponse = await fetch(apiUrl, { headers: { accept: "application/json", "user-agent": "Mozilla/5.0" } });
      let empresaBrasilApi = null;
      if (apiResponse.ok) {
        const data = await apiResponse.json(); empresaBrasilApi = mapBrasilApiToEmpresa(data);
        if (ctx && typeof ctx.waitUntil === "function") {
          const responseToCache = new Response(JSON.stringify(data), { headers: { "content-type": "application/json; charset=UTF-8", "cache-control": "public, max-age=86400" } });
          ctx.waitUntil(caches.default.put(cacheKey, responseToCache));
        }
      }
      const mergedBrasil = mergeEmpresa(empresaBrasilApi, empresaCnpja);
      if (!mergedBrasil || isEnderecoIncompleto(mergedBrasil)) {
        const empresaReceitaWs = await fetchEmpresaFromReceitaWs(cnpjDigits); const mergedReceita = mergeEmpresa(mergedBrasil, empresaReceitaWs);
        if (mergedReceita && !isEnderecoIncompleto(mergedReceita)) return mergedReceita;
        const empresaBiz = await fetchEmpresaFromCnpjBiz(cnpjDigits); return mergeEmpresa(mergedReceita, empresaBiz);
      }
      return mergedBrasil;
    };

    const requestedCnpj = url.searchParams.get("cnpj") || EMBEDDED_DEFAULT_CNPJ || (env && env.DEFAULT_CNPJ) || normalizeCnpj(fallbackEmpresa.cnpj);
    const empresa = (await fetchEmpresaByCnpj(requestedCnpj)) || fallbackEmpresa;

    function renderPage({ title, description, body, noindex = false }) {
      return new Response(
        `<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${escapeHtml(title)}</title>
  <meta name="description" content="${escapeHtml(description)}" />
  ${noindex ? '<meta name="robots" content="noindex, nofollow" />' : '<meta name="robots" content="index, follow" />'}
  ${getFacebookDomainVerificationMeta()}
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    :root {
      --bg: #0a0f14; --bg-2: #0f1720; --panel: rgba(17, 24, 39, 0.78); --panel-2: rgba(255, 255, 255, 0.03); --stroke: rgba(255, 255, 255, 0.08); --text: #e5e7eb; --muted: #94a3b8; --white: #ffffff; --primary: #19c37d; --primary-2: #14a86c; --glow: rgba(25, 195, 125, 0.18); --shadow: 0 20px 60px rgba(0, 0, 0, 0.35); --radius-xl: 26px; --radius-lg: 20px; --radius-md: 16px; --container: 1180px;
    }
    html { scroll-behavior: smooth; }
    body {
      font-family: Arial, Helvetica, sans-serif; color: var(--text);
      background: radial-gradient(circle at top left, rgba(25,195,125,0.16), transparent 30%), radial-gradient(circle at bottom right, rgba(25,195,125,0.10), transparent 28%), linear-gradient(180deg, #091018 0%, #0a0f14 100%);
      min-height: 100vh; line-height: 1.6;
    }
    a { color: var(--primary); text-decoration: none; transition: 0.2s ease; }
    a:hover { color: #58efb1; }
    .container { width: 100%; max-width: var(--container); margin: 0 auto; padding: 0 20px; }
    .topbar { position: sticky; top: 0; z-index: 100; background: rgba(8, 13, 18, 0.72); backdrop-filter: blur(14px); border-bottom: 1px solid rgba(255,255,255,0.05); }
    .topbar-inner { min-height: 78px; display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
    .logo-block { display: flex; flex-direction: column; gap: 4px; padding: 16px 0; }
    .logo-block strong { color: var(--white); font-size: 18px; line-height: 1.2; letter-spacing: 0.2px; }
    .logo-block span { color: var(--muted); font-size: 13px; }
    .nav { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; padding: 12px 0; }
    .nav a { color: var(--text); font-size: 14px; padding: 10px 12px; border-radius: 12px; }
    .nav a:hover { background: rgba(255,255,255,0.04); color: var(--white); }
    .hero { position: relative; padding: 70px 0 30px; }
    .hero-grid { display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 24px; align-items: stretch; }
    .glass-card { background: var(--panel); border: 1px solid var(--stroke); box-shadow: var(--shadow); border-radius: var(--radius-xl); overflow: hidden; }
    .hero-main { padding: 38px; position: relative; }
    .hero-main::before { content: ""; position: absolute; inset: 0; background: linear-gradient(135deg, rgba(25,195,125,0.07), transparent 45%); pointer-events: none; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 9px 14px; border: 1px solid rgba(25,195,125,0.22); background: rgba(25,195,125,0.10); color: #9df5cf; border-radius: 999px; font-size: 13px; margin-bottom: 18px; }
    .hero h1 { font-size: 42px; line-height: 1.08; color: var(--white); max-width: 820px; margin-bottom: 14px; }
    .hero p { font-size: 16px; color: var(--muted); max-width: 760px; }
    .hero-actions { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 26px; }
    .btn { display: inline-flex; align-items: center; justify-content: center; min-height: 48px; padding: 0 18px; border-radius: 14px; font-weight: 700; font-size: 14px; border: 1px solid transparent; transition: 0.2s ease; }
    .btn-primary { background: var(--primary); color: #052416; box-shadow: 0 8px 24px var(--glow); }
    .btn-primary:hover { background: var(--primary-2); color: #041b11; transform: translateY(-1px); }
    .btn-secondary { background: rgba(255,255,255,0.03); border-color: rgba(255,255,255,0.08); color: var(--white); }
    .btn-secondary:hover { background: rgba(255,255,255,0.06); color: var(--white); }
    .hero-side { padding: 26px; display: grid; gap: 14px; align-content: center; }
    .mini-stat { border-radius: 18px; border: 1px solid rgba(255,255,255,0.07); background: var(--panel-2); padding: 16px 18px; }
    .mini-stat .label { display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px; }
    .mini-stat .value { display: block; color: var(--white); font-size: 18px; font-weight: 700; line-height: 1.35; }
    section { padding: 24px 0; }
    .section-head { margin-bottom: 20px; }
    .section-title { color: var(--white); font-size: 30px; line-height: 1.15; margin-bottom: 10px; }
    .section-text { color: var(--muted); max-width: 850px; font-size: 15px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 22px; }
    .card { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); box-shadow: var(--shadow); padding: 24px; }
    .card h3 { color: var(--white); font-size: 20px; margin-bottom: 14px; }
    .card p { color: var(--muted); font-size: 15px; }
    .info-list { display: grid; gap: 12px; }
    .info-item { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.05); border-radius: 15px; padding: 13px 14px; }
    .info-item strong { display: block; color: var(--white); font-size: 14px; margin-bottom: 3px; }
    .info-item span { display: block; color: var(--muted); font-size: 14px; word-break: break-word; }
    .card-large { grid-column: span 2; }
    .card-full { grid-column: 1 / -1; }
    .highlight-box { display: grid; gap: 12px; margin-top: 18px; }
    .highlight { display: flex; gap: 12px; align-items: flex-start; padding: 14px; border-radius: 16px; background: rgba(25,195,125,0.06); border: 1px solid rgba(25,195,125,0.12); }
    .highlight-dot { width: 10px; height: 10px; border-radius: 999px; background: var(--primary); box-shadow: 0 0 0 6px rgba(25,195,125,0.10); margin-top: 7px; flex-shrink: 0; }
    .highlight div { color: var(--muted); font-size: 14px; }
    .highlight div strong { color: var(--white); }
    .footer { margin-top: 34px; border-top: 1px solid rgba(255,255,255,0.06); background: rgba(255,255,255,0.02); }
    .footer-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; padding: 34px 0 20px; }
    .footer-col h4 { color: var(--white); font-size: 16px; margin-bottom: 12px; }
    .footer-col p, .footer-col a { color: var(--muted); font-size: 14px; }
    .footer-col a { display: block; margin-bottom: 8px; }
    .copyright { padding: 0 0 28px; color: #6b7280; font-size: 13px; }
    .legal-wrapper { padding: 70px 0; }
    .legal-card { background: var(--panel); border: 1px solid var(--stroke); border-radius: 24px; box-shadow: var(--shadow); padding: 34px; }
    .legal-card h1 { color: var(--white); font-size: 36px; margin-bottom: 10px; }
    .legal-card p { color: var(--muted); font-size: 15px; margin-bottom: 16px; }
    .legal-card h2 { color: var(--white); font-size: 20px; margin: 26px 0 10px; }
    .top-space { margin-top: 12px; }
    @media (max-width: 980px) { .hero-grid, .grid-3, .grid-2, .footer-grid { grid-template-columns: 1fr; } .card-large, .card-full { grid-column: auto; } .hero h1 { font-size: 33px; } }
    @media (max-width: 640px) { .hero-main, .hero-side, .card, .legal-card { padding: 22px; } .hero { padding-top: 34px; } .section-title { font-size: 25px; } .hero h1 { font-size: 28px; } .topbar-inner { align-items: flex-start; } }
  </style>
</head>
<body>
  ${body}
</body>
</html>`,
        { headers: { "content-type": "text/html; charset=UTF-8" } }
      );
    }

    function header() {
      return `
        <header class="topbar">
          <div class="container topbar-inner">
            <div class="logo-block">
              <strong>${escapeHtml(empresa.razaoSocial)}</strong>
              <span>CNPJ ${escapeHtml(empresa.cnpj)}</span>
            </div>
            <nav class="nav">
              <a href="/">Início</a>
              <a href="/#quem-somos">Quem Somos</a>
              <a href="/#dados">Dados Cadastrais</a>
              <a href="/#contato">Contato</a>
              <a href="/privacidade">Privacidade</a>
              <a href="/termos">Termos</a>
            </nav>
          </div>
        </header>
      `;
    }

    function footer() {
      return `
        <footer class="footer">
          <div class="container">
            <div class="footer-grid">
              <div class="footer-col">
                <h4>Localização</h4>
                <p>
                  ${escapeHtml(empresa.endereco.logradouro)}, ${escapeHtml(empresa.endereco.numero)}<br />
                  ${escapeHtml(empresa.endereco.bairro)}<br />
                  ${escapeHtml(empresa.endereco.cidade)} - ${escapeHtml(empresa.endereco.uf)}<br />
                  CEP ${escapeHtml(empresa.endereco.cep)}
                </p>
              </div>
              <div class="footer-col">
                <h4>Contato Direto</h4>
                ${toTelHref(empresa.contato.telefone) ? `<a href="${escapeHtml(toTelHref(empresa.contato.telefone))}">${escapeHtml(empresa.contato.telefone)}</a>` : `<span>${escapeHtml(empresa.contato.telefone)}</span>`}
                <a href="mailto:${escapeHtml(empresa.contato.email)}">${escapeHtml(empresa.contato.email)}</a>
                <p class="top-space">CNPJ ${escapeHtml(empresa.cnpj)}</p>
              </div>
              <div class="footer-col">
                <h4>Legal</h4>
                <a href="/privacidade">Política de Privacidade</a>
                <a href="/termos">Termos de Uso</a>
              </div>
            </div>
            <div class="copyright">
              © ${new Date().getFullYear()} ${escapeHtml(empresa.razaoSocial)}. Todos os direitos reservados.
            </div>
          </div>
        </footer>
      `;
    }

    if (path === "/privacidade") {
      return renderPage({
        title: `Política de Privacidade | ${empresa.razaoSocial}`,
        description: `Política de privacidade institucional da empresa ${empresa.razaoSocial}.`,
        body: `
          ${header()}
          <main class="container legal-wrapper">
            <div class="legal-card">
              <h1>Política de Privacidade</h1>
              <p>Esta página apresenta informações gerais sobre práticas de privacidade, uso técnico do site e tratamento mínimo de informações necessárias para funcionamento, atendimento e segurança da navegação.</p>
              <h2>1. Coleta de informações</h2>
              <p>Poderão ser coletadas informações fornecidas voluntariamente pelo visitante em eventuais contatos, além de dados técnicos básicos de navegação, quando necessários para segurança, estabilidade e melhoria da experiência no site.</p>
              <h2>2. Utilização das informações</h2>
              <p>As informações poderão ser utilizadas para atendimento, retorno de solicitações, controle interno, funcionamento técnico do ambiente e proteção contra uso indevido.</p>
              <h2>3. Compartilhamento</h2>
              <p>Não há comercialização de dados pessoais. Eventual compartilhamento poderá ocorrer somente quando necessário ao cumprimento de obrigação legal, regulatória ou operação técnica indispensável.</p>
              <h2>4. Segurança</h2>
              <p>São adotadas medidas técnicas e organizacionais razoáveis para preservação da integridade e da segurança das informações tratadas neste ambiente digital.</p>
              <h2>5. Contato</h2>
              <p>Para assuntos relacionados a esta política, o visitante poderá utilizar os canais informados na página principal.</p>
            </div>
          </main>
          ${footer()}
        `,
      });
    }

    if (path === "/termos") {
      return renderPage({
        title: `Termos de Uso | ${empresa.razaoSocial}`,
        description: `Termos de uso institucionais da empresa ${empresa.razaoSocial}.`,
        body: `
          ${header()}
          <main class="container legal-wrapper">
            <div class="legal-card">
              <h1>Termos de Uso</h1>
              <p>Ao acessar este site, o visitante concorda com as condições gerais de navegação, uso responsável do conteúdo e observância da legislação aplicável.</p>
              <h2>1. Finalidade do site</h2>
              <p>Este ambiente possui caráter institucional e informativo, com a finalidade de apresentar dados cadastrais, formas de contato e informações gerais relacionadas à empresa.</p>
              <h2>2. Uso adequado</h2>
              <p>O usuário compromete-se a utilizar o site de forma lícita, ética e sem qualquer prática que possa comprometer a disponibilidade, a segurança ou a integridade da estrutura apresentada.</p>
              <h2>3. Atualização de conteúdo</h2>
              <p>As informações exibidas podem ser atualizadas, alteradas ou removidas a qualquer tempo, sem aviso prévio, sempre que necessário para manutenção, adequação ou revisão.</p>
              <h2>4. Limitações</h2>
              <p>O conteúdo deste site possui natureza institucional e informativa, não substituindo validações formais quando estas forem exigidas em situações específicas.</p>
              <h2>5. Contato</h2>
              <p>Para esclarecimentos adicionais, utilize os meios de contato apresentados na página inicial.</p>
            </div>
          </main>
          ${footer()}
        `,
      });
    }

    const enderecoCompleto = `${empresa.endereco.logradouro}, ${empresa.endereco.numero} - ${empresa.endereco.bairro}, ${empresa.endereco.cidade} - ${empresa.endereco.uf}, ${empresa.endereco.cep}`;
    const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(enderecoCompleto)}`;

    const atividadesSecundariasHtml = empresa.atividadesSecundarias && empresa.atividadesSecundarias.length
        ? `<ul style="margin-top:10px; padding-left: 18px; color: var(--muted);"><li style="margin: 6px 0;">` + empresa.atividadesSecundarias.map((a) => `${escapeHtml(a)}`).join("</li><li style='margin: 6px 0;'>") + `</li></ul>`
        : `<p style="color: var(--muted); font-size: 15px;">Não informado</p>`;

    const homeBody = `
      ${header()}
      <main>
        <section class="hero">
          <div class="container">
            <div class="hero-grid">
              <div class="glass-card hero-main">
                <div class="badge">Empresa ativa • MEI • ${escapeHtml(empresa.endereco.cidade)} / ${escapeHtml(empresa.endereco.uf)}</div>
                <h1>${escapeHtml(empresa.razaoSocial)}</h1>
                <p>Página institucional desenvolvida para apresentar informações cadastrais, meios de contato e estrutura básica da empresa com visual dark moderno, navegação elegante e layout profissional.</p>
                <div class="hero-actions">
                  <a href="#dados" class="btn btn-primary">Ver dados cadastrais</a>
                  <a href="#contato" class="btn btn-secondary">Entrar em contato</a>
                </div>
                <div class="highlight-box">
                  <div class="highlight"><span class="highlight-dot"></span><div><strong>Situação cadastral:</strong> ${escapeHtml(empresa.situacao)}</div></div>
                  <div class="highlight"><span class="highlight-dot"></span><div><strong>Data de abertura:</strong> ${escapeHtml(empresa.abertura)}</div></div>
                  <div class="highlight"><span class="highlight-dot"></span><div><strong>Atividade principal:</strong> ${escapeHtml(empresa.cnaePrincipal)}</div></div>
                </div>
              </div>
              <div class="glass-card hero-side">
                <div class="mini-stat"><span class="label">CNPJ</span><span class="value">${escapeHtml(empresa.cnpj)}</span></div>
                <div class="mini-stat"><span class="label">Situação</span><span class="value">${escapeHtml(empresa.situacao)}</span></div>
                <div class="mini-stat"><span class="label">Abertura</span><span class="value">${escapeHtml(empresa.abertura)}</span></div>
                <div class="mini-stat"><span class="label">Capital Social</span><span class="value">${escapeHtml(empresa.capitalSocial)}</span></div>
              </div>
            </div>
          </div>
        </section>
        <section id="quem-somos">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Quem Somos</h2><p class="section-text">Informações gerais e cadastrais exibidas de forma clara, com foco institucional e navegação simples.</p></div>
            <div class="grid-3">
              <div class="card"><h3>Natureza Jurídica</h3><p>${escapeHtml(empresa.naturezaJuridica)}</p></div>
              <div class="card"><h3>Porte</h3><p>${escapeHtml(empresa.porte)}</p></div>
              <div class="card"><h3>MEI</h3><p>${escapeHtml(empresa.mei)}</p></div>
            </div>
          </div>
        </section>
        <section id="dados">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Dados Cadastrais</h2><p class="section-text">Informações institucionais para referência, contato e validação cadastral.</p></div>
            <div class="grid-2">
              <div class="card">
                <h3>Identificação</h3>
                <div class="info-list">
                  <div class="info-item"><strong>Razão social</strong><span>${escapeHtml(empresa.razaoSocial)}</span></div>
                  <div class="info-item"><strong>Nome fantasia</strong><span>${escapeHtml(empresa.nomeFantasia)}</span></div>
                  <div class="info-item"><strong>CNPJ</strong><span>${escapeHtml(empresa.cnpj)}</span></div>
                  <div class="info-item"><strong>Situação</strong><span>${escapeHtml(empresa.situacao)}</span></div>
                  <div class="info-item"><strong>Data de abertura</strong><span>${escapeHtml(empresa.abertura)}</span></div>
                </div>
              </div>
              <div class="card">
                <h3>Atividades</h3>
                <div class="info-list">
                  <div class="info-item"><strong>CNAE principal</strong><span>${escapeHtml(empresa.cnaePrincipal)}</span></div>
                  <div class="info-item"><strong>Atividades secundárias</strong><span>${atividadesSecundariasHtml}</span></div>
                </div>
              </div>
              <div class="card card-large">
                <h3>Endereço</h3>
                <div class="info-list">
                  <div class="info-item"><strong>Logradouro</strong><span>${escapeHtml(empresa.endereco.logradouro)}</span></div>
                  <div class="info-item"><strong>Número</strong><span>${escapeHtml(empresa.endereco.numero)}</span></div>
                  <div class="info-item"><strong>Complemento</strong><span>${escapeHtml(empresa.endereco.complemento || "—")}</span></div>
                  <div class="info-item"><strong>Bairro</strong><span>${escapeHtml(empresa.endereco.bairro)}</span></div>
                  <div class="info-item"><strong>Cidade / UF</strong><span>${escapeHtml(empresa.endereco.cidade)} / ${escapeHtml(empresa.endereco.uf)}</span></div>
                  <div class="info-item"><strong>CEP</strong><span>${escapeHtml(empresa.endereco.cep)}</span></div>
                  <div class="hero-actions" style="margin-top: 16px;"><a class="btn btn-secondary" href="${mapsUrl}" target="_blank" rel="noopener noreferrer">Ver no mapa</a></div>
                </div>
              </div>
              <div class="card">
                <h3>Enquadramento</h3>
                <div class="info-list">
                  <div class="info-item"><strong>Capital social</strong><span>${escapeHtml(empresa.capitalSocial)}</span></div>
                  <div class="info-item"><strong>Porte</strong><span>${escapeHtml(empresa.porte)}</span></div>
                  <div class="info-item"><strong>MEI</strong><span>${escapeHtml(empresa.mei)}</span></div>
                </div>
              </div>
            </div>
          </div>
        </section>
        <section id="contato">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Contato</h2><p class="section-text">Utilize os canais abaixo para contato direto.</p></div>
            <div class="grid-3">
              <div class="card"><h3>Telefone</h3><p>${toTelHref(empresa.contato.telefone) ? `<a href="${escapeHtml(toTelHref(empresa.contato.telefone))}">${escapeHtml(empresa.contato.telefone)}</a>` : `${escapeHtml(empresa.contato.telefone)}`}</p></div>
              <div class="card"><h3>E-mail</h3><p><a href="mailto:${escapeHtml(empresa.contato.email)}">${escapeHtml(empresa.contato.email)}</a></p></div>
              <div class="card"><h3>Endereço</h3><p>${escapeHtml(enderecoCompleto)}</p><div class="hero-actions" style="margin-top: 14px;"><a class="btn btn-primary" href="${mapsUrl}" target="_blank" rel="noopener noreferrer">Abrir no Google Maps</a></div></div>
            </div>
          </div>
        </section>
      </main>
      ${footer()}
    `;

    if (path === "/") {
      return renderPage({ title: `${empresa.razaoSocial} | CNPJ ${empresa.cnpj}`, description: `Página institucional da empresa ${empresa.razaoSocial} (CNPJ ${empresa.cnpj}).`, body: homeBody });
    }

    return renderPage({
      title: `Página não encontrada | ${empresa.razaoSocial}`, description: `Conteúdo não encontrado.`,
      body: `${header()}<main class="container legal-wrapper"><div class="legal-card"><h1>404 - Página não encontrada</h1><p>O conteúdo solicitado não está disponível.</p><div class="hero-actions"><a href="/" class="btn btn-primary">Voltar para o início</a></div></div></main>${footer()}`,
      noindex: true,
    });
  },
};
"""

# Padrões de mutação — capturam o prefixo (`const NAME = `) + literal entre
# aspas + `;` para que o ``lambda`` substitua só o valor.
_CNPJ_CONST_RE = re.compile(
    r'(const\s+EMBEDDED_DEFAULT_CNPJ\s*=\s*)"[^"]*"(\s*;)'
)
_FB_CONST_RE = re.compile(
    r'(const\s+EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION\s*=\s*)"[^"]*"(\s*;)'
)


class CloudflareDeployerError(RuntimeError):
    """Erro no pipeline de render/deploy do Worker."""


async def fetch_account_id_from_token(
    api_token: str, *, timeout_s: float = 15.0
) -> Optional[str]:
    """Descobre o Account ID a partir SÓ do API Token (GET /accounts) — o
    operador cola apenas o token e o sistema acha a conta. Devolve o 1º account
    id acessível pelo token, ou None se indeterminável."""
    if not (api_token or "").strip():
        return None
    url = f"{CLOUDFLARE_API_BASE.rstrip('/')}/accounts"
    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(url, headers=headers, params={"per_page": 50})
            data = r.json()
        if r.status_code != 200 or not data.get("success"):
            logger.debug(
                "fetch_account_id_from_token: HTTP %s %s",
                r.status_code, data.get("errors"),
            )
            return None
        results = data.get("result") or []
        return (results[0].get("id") if results else None) or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_account_id_from_token falhou: %s", exc)
        return None


async def fetch_account_subdomain(
    account_id: str, api_token: str, *, timeout_s: float = 15.0
) -> Optional[str]:
    """Subdomínio workers.dev registrado na conta (ou None se indeterminável).

    Standalone (não exige instanciar o deployer) p/ o PREFLIGHT checar, ANTES de
    gastar a run, se ``CLOUDFLARE_WORKERS_SUBDOMAIN`` bate com a conta — o domínio
    é ``{slug}.{CLOUDFLARE_WORKERS_SUBDOMAIN}.workers.dev`` e, se a conta servir
    OUTRO subdomínio, o Worker publica no host errado e o Meta nunca verifica
    (visto 2026-06: conta servia *.myperfilprofissional, domínio era *.corporateprofile).
    """
    if not ((account_id or "").strip() and (api_token or "").strip()):
        return None
    url = f"{CLOUDFLARE_API_BASE.rstrip('/')}/accounts/{account_id}/workers/subdomain"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Accept": "application/json",
                },
            )
            data = r.json()
        return ((data.get("result") or {}).get("subdomain")) or None
    except Exception as exc:  # noqa: BLE001 — indeterminável não derruba o preflight
        logger.debug("fetch_account_subdomain falhou: %s", exc)
        return None


async def validate_cloudflare_ready(
    account_id: Optional[str],
    api_token: Optional[str],
    worker_name: Optional[str],
) -> dict[str, Any]:
    """Cloudflare PRONTO para uma run? Checa (1) presença das 3 credenciais e
    (2) token ativo + conta acessível (ao vivo). Usado para ABORTAR a run ANTES
    de gastar SMS/conta — o operador exige que a run NUNCA inicie sem credenciais
    preenchidas/válidas (2026-06).

    Retorna ``{"ok", "detail", "subdomain"}``. ``subdomain`` é o subdomínio
    workers.dev REAL da conta (ou ``None`` se indeterminável). O domínio da run é
    DERIVADO desse subdomínio (em ``run_service``), então NÃO há mais comparação
    com ``CLOUDFLARE_WORKERS_SUBDOMAIN`` (.env): trocar a conta CF no painel passou
    a "just work" — antes o operador trocava a conta e TODA run abortava porque o
    ``.env`` ficava velho (era a causa nº1 de runs barradas, vista 3× em jun/2026:
    myperfilprofissional → siteprofissionalperfil → empresarialperfil). O ``.env``
    virou só fallback p/ quando a API não devolve o subdomínio (hiccup de rede); o
    deploy ainda tem a guarda de host (``_assert_domain_account_match``) como
    backstop final."""
    # worker_name (nome base) NÃO é mais exigido: em domínio workers.dev o nome
    # do script é DERIVADO do domínio e o subdomínio vem da API da conta
    # (fetch_account_subdomain). Basta account_id + api_token — o operador não
    # precisa preencher nada além disso ao trocar de conta Cloudflare.
    if not ((account_id or "").strip() and (api_token or "").strip()):
        return {
            "ok": False,
            "detail": "Cloudflare NÃO preenchido (account_id + api_token).",
            "subdomain": None,
        }
    res = await test_cloudflare_creds(account_id, api_token)
    if not res.get("ok"):
        return {
            "ok": False,
            "detail": f"credencial Cloudflare inválida: {res.get('message')}",
            "subdomain": None,
        }
    # Tenta ler o subdomínio REAL da conta pela API. Pode voltar None (token sem
    # permissão de leitura OU conta sem subdomínio) — NÃO falha aqui; o
    # run_service usa o subdomínio informado manualmente na conta como fallback,
    # e só aborta se não houver nenhum (nunca usa o 'perfilprofissional' do .env).
    actual = await fetch_account_subdomain(account_id, api_token)
    return {
        "ok": True,
        "detail": (
            f"ok (subdomínio da conta: '{actual}')"
            if actual
            else "ok (token não leu o subdomínio — usará o informado na conta)"
        ),
        "subdomain": actual,
    }


@http_retry(
    exceptions=(httpx.TransportError, httpx.TimeoutException),
    logger=logger,
)
async def test_cloudflare_creds(
    account_id: str,
    api_token: str,
    *,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    """Valida (account_id, api_token) consultando ``/user/tokens/verify``.

    Retorna ``{"ok": bool, "message": str, "status_payload": dict|None}``.
    ``ok=True`` quando o token está ativo. Não lança — converte falhas
    HTTP/transporte em ``ok=False`` para uso direto na UI.

    H2 — Retry (3×) em erros de rede/timeout (transient).
    4xx HTTP status não dispara retry (erro do cliente, não transient).
    """
    if not (account_id or "").strip():
        return {"ok": False, "message": "Account ID vazio.", "status_payload": None}
    if not (api_token or "").strip():
        return {"ok": False, "message": "API Token vazio.", "status_payload": None}

    base = CLOUDFLARE_API_BASE.rstrip("/")
    verify_url = f"{base}/user/tokens/verify"
    accounts_url = f"{base}/accounts/{account_id}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r1 = await client.get(verify_url, headers=headers)
            try:
                payload = r1.json()
            except Exception as exc:  # noqa: BLE001
                logger.debug("cf verify response: JSON parse falhou err=%s", type(exc).__name__)
                payload = {"raw": r1.text}
            if r1.status_code != 200 or not payload.get("success", False):
                return {
                    "ok": False,
                    "message": (
                        f"Token inválido (HTTP {r1.status_code}): "
                        f"{payload.get('errors') or payload.get('raw') or payload}"
                    ),
                    "status_payload": payload,
                }
            # Checa o status do token (active / disabled / expired).
            status = ((payload.get("result") or {}).get("status") or "").lower()
            if status and status != "active":
                return {
                    "ok": False,
                    "message": f"Token presente mas inativo (status={status}).",
                    "status_payload": payload,
                }
            # Confirma que a conta é acessível.
            r2 = await client.get(accounts_url, headers=headers)
            if r2.status_code != 200:
                return {
                    "ok": False,
                    "message": (
                        f"Token OK, mas account_id {account_id!r} "
                        f"inacessível (HTTP {r2.status_code})."
                    ),
                    "status_payload": payload,
                }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "message": f"Cloudflare inacessível: {type(exc).__name__}: {exc}",
            "status_payload": None,
        }
    return {
        "ok": True,
        "message": "Credenciais válidas — token ativo e conta acessível.",
        "status_payload": payload,
    }


class CloudflareDeployer:
    """Mutação + deploy do Cloudflare Worker para o domínio do cliente."""

    def __init__(
        self,
        *,
        account_id: str = CLOUDFLARE_ACCOUNT_ID,
        api_token: str = CLOUDFLARE_API_TOKEN,
        worker_name: str = CLOUDFLARE_SCRIPT_NAME,
        api_base: str = CLOUDFLARE_API_BASE,
        compatibility_date: str = CLOUDFLARE_COMPATIBILITY_DATE,
        timeout_s: float = CLOUDFLARE_REQUEST_TIMEOUT_S,
    ) -> None:
        self.account_id = account_id
        self.api_token = api_token
        self.worker_name = worker_name
        self.api_base = api_base.rstrip("/")
        self.compatibility_date = compatibility_date
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.account_id and self.api_token and self.worker_name)

    # ------------------------------------------------------------------
    # Mutação cirúrgica
    # ------------------------------------------------------------------
    @classmethod
    def render_worker(cls, *, cnpj_digits: str, token_hash: str) -> str:
        """Aplica as DUAS substituições no template e devolve o JS final.

        Valida invariantes:
          * ``cnpj_digits`` deve ser 14 dígitos numéricos (apenas dígitos —
            o caller é responsável por limpar pontuação).
          * ``token_hash`` deve ser alfanumérico (Meta usa A-Za-z0-9_-).
        """
        if not re.fullmatch(r"\d{14}", cnpj_digits or ""):
            raise CloudflareDeployerError(
                f"cnpj_digits inválido (esperado 14 dígitos): {cnpj_digits!r}"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", token_hash or ""):
            raise CloudflareDeployerError(
                f"token_hash inválido (alfanumérico, >=8 chars): {token_hash!r}"
            )

        src = _WORKER_TEMPLATE
        src, n1 = _CNPJ_CONST_RE.subn(
            lambda m: f'{m.group(1)}"{cnpj_digits}"{m.group(2)}', src
        )
        if n1 != 1:
            raise CloudflareDeployerError(
                f"EMBEDDED_DEFAULT_CNPJ: esperava 1 ocorrência, encontrou {n1}."
            )
        src, n2 = _FB_CONST_RE.subn(
            lambda m: f'{m.group(1)}"{token_hash}"{m.group(2)}', src
        )
        if n2 != 1:
            raise CloudflareDeployerError(
                f"EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION: esperava 1, encontrou {n2}."
            )
        return src

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------
    async def deploy(
        self, domain: str, *, cnpj_digits: str, token_hash: str
    ) -> str:
        """Renderiza + faz PUT do Worker. Retorna o nome do script publicado.

        workers.dev serve o script em ``{script}.{subdomínio}.workers.dev`` — o
        NOME do script TEM que ser o rótulo à esquerda do domínio alvo (o slug
        do cliente). Antes usávamos um ``worker_name`` FIXO (ex.: "wk") → o site
        ficava em ``wk.<sub>.workers.dev`` e o probe do Meta batia em
        ``<slug>.<sub>.workers.dev`` (404) — o domínio NUNCA verificava. Deriva
        o nome do domínio; mantém o fixo só p/ domínio custom (sem workers.dev).
        """
        derived = self._script_name_for(domain)
        if derived and derived != self.worker_name:
            logger.info(
                "Cloudflare: nome do script = %r (derivado do domínio %s; o "
                "worker_name fixo %r é ignorado em domínio workers.dev).",
                derived, domain, self.worker_name,
            )
            self.worker_name = derived
        worker_source = self.render_worker(
            cnpj_digits=cnpj_digits, token_hash=token_hash
        )
        name = await self._put_worker(domain, worker_source)
        # GUARDA conta×subdomínio: o PUT 'sucede' mesmo quando a conta do
        # token NÃO é a dona do subdomínio workers.dev do domínio alvo — o
        # script vai para a conta errada e o site NO AR continua o antigo
        # (sem a metatag), visto em produção 2026-06: probe 8× sem a tag.
        await self._assert_domain_account_match(domain)
        return name

    @staticmethod
    def _script_name_for(domain: str) -> Optional[str]:
        """Nome do script Worker p/ o domínio alvo.

        workers.dev serve ``{script}.{subdomínio}.workers.dev`` → o script é o
        rótulo à ESQUERDA. ``camila.corporateprofile.workers.dev`` → ``camila``.
        Devolve None p/ domínio custom (aí mantém o ``worker_name`` fixo).
        """
        parts = (domain or "").lower().strip().split(".")
        if len(parts) >= 4 and parts[-2:] == ["workers", "dev"]:
            return parts[0] or None
        return None

    @staticmethod
    def _expected_subdomain(domain: str) -> Optional[str]:
        """Extrai o subdomínio workers.dev do domínio alvo.

        ``foo.perfilprofissional.workers.dev`` → ``perfilprofissional``.
        Devolve None para domínios custom (sem invariante de subdomínio).
        """
        parts = (domain or "").lower().strip().split(".")
        if len(parts) >= 4 and parts[-2:] == ["workers", "dev"]:
            return parts[-3]
        return None

    async def _account_subdomain(self) -> Optional[str]:
        """Subdomínio workers.dev registrado na conta do token (ou None)."""
        return await fetch_account_subdomain(
            self.account_id, self.api_token, timeout_s=self.timeout_s
        )

    async def _assert_domain_account_match(self, domain: str) -> None:
        expected = self._expected_subdomain(domain)
        if not expected:
            return  # domínio custom — sem invariante a validar
        actual = await self._account_subdomain()
        if actual and actual != expected:
            raise CloudflareDeployerError(
                f"Worker publicado na conta Cloudflare ERRADA: o domínio alvo "
                f"é *.{expected}.workers.dev, mas a conta {self.account_id} "
                f"serve *.{actual}.workers.dev — o site no ar continua o "
                f"ANTIGO (sem a metatag). Corrija o Account ID/API Token na "
                f"UI para a conta dona de '{expected}'."
            )
        if actual is None:
            logger.warning(
                "Não foi possível confirmar o subdomínio workers.dev da conta "
                "(token sem permissão Workers Subdomain:Read?) — se a metatag "
                "não aparecer no ar, o deploy pode estar indo para a conta "
                "errada."
            )

    async def _put_worker(self, domain: str, worker_source: str) -> str:
        if not self.enabled:
            raise CloudflareDeployerError(
                "Credenciais Cloudflare ausentes "
                "(CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_WORKER_NAME)."
            )

        url = (
            f"{self.api_base}/accounts/{self.account_id}"
            f"/workers/scripts/{self.worker_name}"
        )
        metadata = json.dumps(
            {
                "main_module": MAIN_MODULE_NAME,
                "compatibility_date": self.compatibility_date,
            }
        )
        # Contrato multipart CF Workers — ver constantes no topo do arquivo.
        # O nome da parte (`MAIN_MODULE_NAME`) precisa bater com o
        # `main_module` da metadata; o Content-Type tem que ser
        # ``application/javascript+module`` para ES modules.
        files = {
            "metadata": (None, metadata, METADATA_CT),
            MAIN_MODULE_NAME: (
                MAIN_MODULE_NAME,
                worker_source.encode("utf-8"),
                MODULE_CT,
            ),
        }
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }

        logger.info(
            "PUT Worker → %s (script=%s, %s bytes, domain alvo=%s)",
            url, self.worker_name, len(worker_source), domain,
        )

        # H2 — retry só em erro de rede/timeout. Status HTTP 5xx do CF é
        # raro e geralmente sintoma de problema persistente; o caller (UI)
        # já tem botão "tentar de novo".
        r = await self._do_put(url, headers, files)

        try:
            payload: dict[str, Any] = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cf put response: JSON parse falhou err=%s", type(exc).__name__)
            payload = {"raw": r.text}

        if r.status_code != 200 or not payload.get("success", False):
            # Log estruturado antes de lançar — ajuda diagnóstico sem expor tokens
            logger.error(
                "Cloudflare PUT falhou status=%d success=%s errors=%s",
                r.status_code, payload.get("success", False),
                payload.get("errors", "N/A"),
            )
            raise CloudflareDeployerError(
                f"Cloudflare PUT falhou (HTTP {r.status_code}): {payload}"
            )

        logger.info(
            "Worker %r publicado — habilitando a rota workers.dev do script.",
            self.worker_name,
        )
        # CRÍTICO: um script criado via API PUT NÃO tem a rota
        # <script>.<subdomínio>.workers.dev habilitada automaticamente — o
        # host devolve 404 e o site nunca serve a metatag (visto em produção
        # 2026-06, conta Cleber: probe 8× HTTP 404 em corporateprofile). O
        # deploy manual pelo painel auto-habilita; o PUT não. Habilita aqui.
        await self._enable_workers_dev_subdomain()
        return self.worker_name

    async def _enable_workers_dev_subdomain(self) -> None:
        """Habilita ``<script>.<conta>.workers.dev`` para o script (best-effort).

        Sem isso o host responde 404 mesmo com o script publicado. Idempotente
        no lado do Cloudflare (re-habilitar não quebra).
        """
        url = (
            f"{self.api_base}/accounts/{self.account_id}"
            f"/workers/scripts/{self.worker_name}/subdomain"
        )
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(
                    url, headers=headers,
                    json={"enabled": True, "previews_enabled": False},
                )
            ok = False
            try:
                ok = bool((r.json() or {}).get("success"))
            except Exception:  # noqa: BLE001
                ok = r.status_code == 200
            if ok:
                logger.info(
                    "Rota workers.dev habilitada para o script %r.", self.worker_name
                )
            else:
                logger.warning(
                    "Não confirmei a habilitação da rota workers.dev (HTTP %s) — "
                    "se o probe der 404, habilite o subdomínio do script no "
                    "painel (Workers → %s → Settings → Enable route).",
                    r.status_code, self.worker_name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Falha ao habilitar a rota workers.dev do script (%s) — "
                "o host pode responder 404 até habilitar manualmente.", exc,
            )

    @http_retry(
        exceptions=(httpx.TransportError, httpx.TimeoutException),
        logger=logger,
    )
    async def _do_put(
        self,
        url: str,
        headers: dict[str, str],
        files: dict[str, Any],
    ) -> httpx.Response:
        """H2 — PUT multipart com retry (3×) em erros de rede."""
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            return await client.put(url, headers=headers, files=files)


# ---------------------------------------------------------------------------
# Pool de contas Cloudflare — rotação automática a cada N deploys
# ---------------------------------------------------------------------------
class CloudflareAccountPool:
    """Mantém um pool de contas Cloudflare e rotaciona automaticamente.

    Configuração via env:
      * ``CLOUDFLARE_ACCOUNT_IDS``    (CSV)
      * ``CLOUDFLARE_API_TOKENS``     (CSV — mesmo tamanho dos IDs)
      * ``CLOUDFLARE_SCRIPT_NAME``    (nome do worker — único para todas)
      * ``CLOUDFLARE_ROTATION_THRESHOLD`` (default 10 deploys por conta)

    Persiste o estado em ``CLOUDFLARE_ROTATION_STATE_PATH`` (JSON com
    ``{"index": int, "deploys_on_current": int}``). Write atômico via
    rename evita corrupção em runs concorrentes.
    """

    def __init__(
        self,
        account_ids: Optional[list[str]] = None,
        api_tokens: Optional[list[str]] = None,
        *,
        script_name: str = CLOUDFLARE_SCRIPT_NAME,
        threshold: int = CLOUDFLARE_ROTATION_THRESHOLD,
        state_path: Path = CLOUDFLARE_ROTATION_STATE_PATH,
    ) -> None:
        ids = account_ids if account_ids is not None else list(CLOUDFLARE_ACCOUNT_IDS)
        tokens = api_tokens if api_tokens is not None else list(CLOUDFLARE_API_TOKENS)
        if len(ids) != len(tokens):
            raise CloudflareDeployerError(
                f"Pool CF mal configurado: {len(ids)} account_ids vs "
                f"{len(tokens)} api_tokens — listas precisam ter mesmo tamanho."
            )
        self.accounts: list[tuple[str, str]] = list(zip(ids, tokens))
        self.script_name = script_name
        self.threshold = max(1, threshold)
        self.state_path = Path(state_path)

    # ---- estado --------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self.accounts)

    @property
    def enabled(self) -> bool:
        """True quando há ao menos 1 conta no pool E um script name."""
        return self.size > 0 and bool(self.script_name)

    def _load_state(self) -> dict[str, int]:
        """Lê o JSON de métricas; defaults zerados em ausência/corrupção.

        Schema:
            current_index       — índice da conta ativa (0-based)
            deploys_on_current  — contador para rotação (0..threshold-1)
            total_deploys       — contador lifetime (nunca zera; auditoria)

        Em caso de arquivo faltante ou corrupto, loga INFO e devolve defaults.
        """
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {
                "current_index": int(
                    data.get("current_index", data.get("index", 0))
                ),
                "deploys_on_current": int(data.get("deploys_on_current", 0)),
                "total_deploys": int(data.get("total_deploys", 0)),
            }
        except FileNotFoundError:
            # Primeira execução ou state deletado — esperado
            return {"current_index": 0, "deploys_on_current": 0, "total_deploys": 0}
        except (json.JSONDecodeError, ValueError) as exc:
            # Corrupção do arquivo — log ajuda a diagnosticar
            logger.info(
                "Pool CF: estado corrupto ou inválido — resetando. "
                "err=%s path=%s",
                type(exc).__name__, self.state_path,
            )
            return {"current_index": 0, "deploys_on_current": 0, "total_deploys": 0}

    def _save_state(self, state: dict[str, int]) -> None:
        """Persiste o estado em arquivo JSON — escrita atômica via tmp+rename.

        Em caso de erro (disco cheio, permissão, etc), loga um WARNING
        estruturado. NÃO levanta exceção — falha na persistence é
        preocupante mas não deve quebrar o deploy.
        """
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)  # atômico no POSIX
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Pool CF: falha ao persistir métricas de rotação — "
                "err=%s path=%s state_json=%s",
                type(exc).__name__, self.state_path, state,
            )

    # ---- API pública ---------------------------------------------------
    def read_metrics(self) -> dict[str, int]:
        """Read-only acesso ao estado atual — usado pela UI Streamlit."""
        return self._load_state()

    def current_index(self) -> int:
        return self._load_state()["current_index"] % self.size

    def current_account(self) -> tuple[str, str]:
        """Devolve ``(account_id, api_token)`` da conta ativa no momento."""
        if not self.enabled:
            raise CloudflareDeployerError("Pool CF vazio — nada para devolver.")
        return self.accounts[self.current_index()]

    def current_deployer(self) -> CloudflareDeployer:
        account_id, api_token = self.current_account()
        return CloudflareDeployer(
            account_id=account_id,
            api_token=api_token,
            worker_name=self.script_name,
        )

    def mark_successful_deploy(self) -> dict[str, int]:
        """Incrementa contadores e rotaciona ao atingir o threshold.

        NÃO levanta em falha de persistence — _save_state() já loga.
        Retorna o estado novo (em memória) mesmo que write tenha falhado.
        """
        if not self.enabled:
            return {"current_index": 0, "deploys_on_current": 0, "total_deploys": 0}
        state = self._load_state()
        state["deploys_on_current"] += 1
        state["total_deploys"] += 1
        if state["deploys_on_current"] >= self.threshold:
            old_idx = state["current_index"] % self.size
            state["current_index"] = (state["current_index"] + 1) % self.size
            state["deploys_on_current"] = 0
            logger.info(
                "Pool CF: rotacionando conta %d → %d (atingiu %d deploys).",
                old_idx + 1, state["current_index"] + 1, self.threshold,
            )
        self._save_state(state)
        return state


# ===========================================================================
# CloudflareDomainDeployer — host por DOMÍNIO PRÓPRIO (modelo wildcard, igual
# ao Netlify atual). 1 zona na conta Cloudflare + 1 Worker multi-tenant + rota
# curinga ``*.dominio/*`` + registro DNS curinga proxied. Cada CNPJ é um
# subdomínio ``slug.dominio`` servido pelo MESMO Worker (escolhe pelo Host).
#
# É ADITIVO: não toca no CloudflareDeployer (workers.dev) nem no fluxo Netlify.
# Só entra em uso quando settings.hosting_provider == 'cloudflare'.
# ===========================================================================
def _cf_script_name_for(domain: str) -> str:
    """Nome estável do Worker para um domínio (1 script por domínio)."""
    base = re.sub(r"[^a-z0-9]+", "-", (domain or "").lower()).strip("-")
    return ("mf-" + base)[:54] or "mf-site"


class CloudflareDomainDeployer:
    """Deploy Cloudflare por domínio próprio (wildcard multi-tenant)."""

    def __init__(
        self,
        account_id: Optional[str],
        api_token: Optional[str],
        *,
        api_base: str = CLOUDFLARE_API_BASE,
        compatibility_date: str = CLOUDFLARE_COMPATIBILITY_DATE,
        timeout_s: float = CLOUDFLARE_REQUEST_TIMEOUT_S,
    ) -> None:
        self.account_id = (account_id or "").strip()
        self.api_token = (api_token or "").strip()
        self.api_base = (api_base or CLOUDFLARE_API_BASE).rstrip("/")
        self.compatibility_date = compatibility_date
        self.timeout_s = float(timeout_s)

    @property
    def enabled(self) -> bool:
        return bool(self.account_id and self.api_token)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }

    # --- Zona ---------------------------------------------------------------
    async def find_zone(self, domain: str) -> Optional[dict]:
        """Lê a zona do domínio na conta (ou None se ainda não existe)."""
        dom = (domain or "").strip().lower().strip(".")
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.get(
                f"{self.api_base}/zones",
                params={"name": dom, "account.id": self.account_id},
                headers=self._headers(),
            )
            data = r.json() if r.content else {}
        res = (data or {}).get("result") or []
        if not res:
            return None
        z = res[0]
        return {
            "zone_id": z.get("id"),
            "nameservers": z.get("name_servers") or [],
            "status": z.get("status"),
        }

    async def ensure_zone(self, domain: str) -> dict:
        """Cria (ou lê) a zona do domínio. Retorna {zone_id, nameservers, status}."""
        dom = (domain or "").strip().lower().strip(".")
        existing = await self.find_zone(dom)
        if existing:
            return existing
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.post(
                f"{self.api_base}/zones",
                headers=self._headers(),
                json={"name": dom, "account": {"id": self.account_id}, "type": "full"},
            )
            data = r.json() if r.content else {}
        if r.status_code not in (200, 201) or not data.get("success", False):
            raise CloudflareDeployerError(
                f"criar zona Cloudflare para {dom!r} falhou (HTTP {r.status_code}): "
                f"{data.get('errors') or data}"
            )
        z = data["result"]
        return {
            "zone_id": z.get("id"),
            "nameservers": z.get("name_servers") or [],
            "status": z.get("status"),
        }

    # --- DNS curinga --------------------------------------------------------
    async def ensure_wildcard_dns(self, zone_id: str, domain: str) -> None:
        """Garante um registro DNS curinga ``*.dominio`` proxied (nuvem laranja).

        O conteúdo do registro é um IP TEST-NET não roteável — a rota do Worker
        intercepta antes de qualquer origem, então o IP nunca é contatado. O que
        importa é o hostname resolver PELA Cloudflare (proxied) para o Worker
        rodar e o SSL universal (*.dominio) cobrir o subdomínio."""
        dom = (domain or "").strip().lower().strip(".")
        name = f"*.{dom}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.get(
                f"{self.api_base}/zones/{zone_id}/dns_records",
                params={"type": "A", "name": name},
                headers=self._headers(),
            )
            data = r.json() if r.content else {}
            if (data or {}).get("result"):
                return  # já existe
            r2 = await c.post(
                f"{self.api_base}/zones/{zone_id}/dns_records",
                headers=self._headers(),
                json={
                    "type": "A",
                    "name": name,
                    "content": "192.0.2.1",
                    "proxied": True,
                    "ttl": 1,
                },
            )
            d2 = r2.json() if r2.content else {}
        if r2.status_code not in (200, 201) or not d2.get("success", False):
            raise CloudflareDeployerError(
                f"criar DNS curinga {name!r} falhou (HTTP {r2.status_code}): "
                f"{d2.get('errors') or d2}"
            )

    # --- Rota do Worker -----------------------------------------------------
    async def ensure_route(self, zone_id: str, domain: str, script_name: str) -> None:
        """Garante a rota ``*.dominio/*`` -> Worker (idempotente)."""
        dom = (domain or "").strip().lower().strip(".")
        pattern = f"*.{dom}/*"
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.get(
                f"{self.api_base}/zones/{zone_id}/workers/routes",
                headers=self._headers(),
            )
            data = r.json() if r.content else {}
            for route in (data or {}).get("result") or []:
                if (route.get("pattern") or "") == pattern:
                    return  # já existe
            r2 = await c.post(
                f"{self.api_base}/zones/{zone_id}/workers/routes",
                headers=self._headers(),
                json={"pattern": pattern, "script": script_name},
            )
            d2 = r2.json() if r2.content else {}
        if r2.status_code not in (200, 201) or not d2.get("success", False):
            raise CloudflareDeployerError(
                f"criar rota {pattern!r} -> {script_name!r} falhou "
                f"(HTTP {r2.status_code}): {d2.get('errors') or d2}"
            )

    # --- PUT do Worker multi-tenant ----------------------------------------
    async def put_multi_worker(self, script_name: str, worker_source: str) -> None:
        url = f"{self.api_base}/accounts/{self.account_id}/workers/scripts/{script_name}"
        metadata = json.dumps({
            "main_module": MAIN_MODULE_NAME,
            "compatibility_date": self.compatibility_date,
        })
        files = {
            "metadata": (None, metadata, METADATA_CT),
            MAIN_MODULE_NAME: (MAIN_MODULE_NAME, worker_source.encode("utf-8"), MODULE_CT),
        }
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.put(url, headers=self._headers(), files=files)
            data = r.json() if r.content else {}
        if r.status_code != 200 or not data.get("success", False):
            raise CloudflareDeployerError(
                f"PUT Worker {script_name!r} falhou (HTTP {r.status_code}): "
                f"{data.get('errors') or data}"
            )

    # --- Fluxo completo -----------------------------------------------------
    async def _relax_zone_security(self, zone_id: str) -> None:
        """Desliga as defesas que DESAFIAM o crawler do Meta (produção 2026-09):
        zona Cloudflare nova nasce com ``security_level=medium`` e
        ``browser_check=on`` (Browser Integrity Check) — o crawler
        facebookexternalhit é barrado e a verificação de domínio dá 'Erro ao
        realizar a consulta' (o Netlify não tinha isso, por isso verificava na
        hora). Para estes sites de verificação segurança não importa. Best-effort,
        não-fatal: se falhar, o deploy segue."""
        for path, value in (
            ("/settings/security_level", "essentially_off"),
            ("/settings/browser_check", "off"),
        ):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                    await c.patch(
                        f"{self.api_base}/zones/{zone_id}{path}",
                        headers=self._headers(),
                        json={"value": value},
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("relax_zone_security %s falhou (%s).", path, exc)

    async def publish_domain(self, domain: str, entries: Any) -> str:
        """Publica/atualiza o Worker multi-tenant do domínio e garante DNS+rota.

        ``entries``: mapa slug->{cnpj,token} (ou lista) de TODOS os CNPJs já
        publicados no domínio. Retorna o nome do script publicado."""
        from core.site_template import render_multi_worker_js  # noqa: PLC0415

        if not self.enabled:
            raise CloudflareDeployerError(
                "Credenciais Cloudflare ausentes (account_id / api_token)."
            )
        dom = (domain or "").strip().lower().strip(".")
        zone = await self.ensure_zone(dom)
        zone_id = zone["zone_id"]
        # Garante que o crawler do Meta não seja desafiado (best-effort).
        await self._relax_zone_security(zone_id)
        if (zone.get("status") or "") != "active":
            raise CloudflareDeployerError(
                f"zona {dom!r} ainda não está ATIVA na Cloudflare (status="
                f"{zone.get('status')!r}) — os nameservers ainda não propagaram."
            )
        script_name = _cf_script_name_for(dom)
        worker_source = render_multi_worker_js(entries)
        await self.put_multi_worker(script_name, worker_source)
        await self.ensure_wildcard_dns(zone_id, dom)
        await self.ensure_route(zone_id, dom, script_name)
        logger.info(
            "Cloudflare (domínio próprio): Worker %r publicado em *.%s (%d CNPJ(s) no mapa).",
            script_name, dom,
            len(entries) if hasattr(entries, "__len__") else 0,
        )
        return script_name


async def validate_cloudflare_domain_ready(
    domain: Optional[str], account_id: Optional[str], api_token: Optional[str]
) -> dict:
    """Cloudflare (domínio próprio) PRONTO para uma run? Checa credenciais +
    zona existente + status ATIVO (nameservers propagados).

    Retorna ``{"ok", "detail", "nameservers"}`` — mesmo contrato de
    ``validate_netlify_ready`` (usado pelo gate do run_service)."""
    dom = (domain or "").strip().lower().strip(".")
    acct = (account_id or "").strip()
    tok = (api_token or "").strip()
    if not (dom and acct and tok):
        return {
            "ok": False,
            "detail": "Cloudflare NÃO configurado (domínio + Account ID + API Token).",
            "nameservers": [],
        }
    dep = CloudflareDomainDeployer(acct, tok)
    try:
        zone = await dep.find_zone(dom)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"erro ao consultar zona Cloudflare: {exc}", "nameservers": []}
    if not zone:
        return {
            "ok": False,
            "detail": (
                f"o domínio {dom!r} ainda não tem zona na Cloudflare — adicione o "
                "domínio no painel (Configurações > Cloudflare) para gerar os nameservers."
            ),
            "nameservers": [],
        }
    ns = zone.get("nameservers") or []
    if (zone.get("status") or "") != "active":
        return {
            "ok": False,
            "detail": (
                f"a zona {dom!r} ainda não está ATIVA na Cloudflare (status="
                f"{zone.get('status')!r}). Coloque os nameservers da Cloudflare na "
                "Hostinger e aguarde a propagação."
            ),
            "nameservers": ns,
        }
    return {
        "ok": True,
        "detail": "ok (zona Cloudflare ativa, nameservers propagados).",
        "nameservers": ns,
    }
