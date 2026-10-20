"""Fonte ÚNICA da página institucional por CNPJ (o "worker").

Este módulo guarda o template do site (o MESMO tema/HTML/lógica de busca de
CNPJ que rodava no Cloudflare Worker) e as funções de mutação/transformação:

  * ``render_worker_js(cnpj_digits, token_hash)`` — injeta o CNPJ e o token de
    verificação do Facebook no template (substituição cirúrgica dos 2 literais).
  * ``to_edge_function(js)`` — adapta o JS do worker (Cloudflare) para uma
    Netlify Edge Function (runtime Deno): troca o wrapper ``export default {
    fetch(request, env, ctx) }`` por ``export default async (request, context)``,
    remove a Cache API da Cloudflare (``caches.default`` não existe no edge da
    Netlify), aponta ``env`` para ``Netlify.env`` e declara ``config.path``.
  * ``render_edge_function(cnpj_digits, token_hash)`` — atalho: muta + adapta.

O conteúdo da página (tema dark, dados cadastrais, /privacidade, /termos e a
metatag ``facebook-domain-verification``) é idêntico ao do worker original.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional


class SiteTemplateError(RuntimeError):
    """Erro na mutação/transformação do template do site."""


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
    const EMBEDDED_EMPRESA = null;
    const EMBEDDED_TEMPLATE = "classic";

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
    // EMBEDDED_EMPRESA: dados do CNPJ JÁ consultados no backend (Python) e
    // embutidos no render — usados PRIMEIRO para não depender da consulta LIVE
    // (que às vezes falha e caía no fallback de exemplo, abortando o deploy da
    // 2ª BM). Só bate nos provedores externos quando não há dados embutidos.
    // PREVIEW ao vivo (aba Gerar Site): ?__preview=<base64 do JSON do empresa>
    // renderiza a página com ESSES dados SEM publicar/salvar nada — usado só
    // pelo iframe do painel para ver as edições em tempo real.
    let __previewEmpresa = null;
    try {
      const __pv = url.searchParams.get("__preview");
      if (__pv) __previewEmpresa = JSON.parse(decodeURIComponent(escape(atob(__pv))));
    } catch (e) { __previewEmpresa = null; }
    const empresa = __previewEmpresa || EMBEDDED_EMPRESA || (await fetchEmpresaByCnpj(requestedCnpj)) || fallbackEmpresa;

    const _CSS_CLASSIC = `* { margin: 0; padding: 0; box-sizing: border-box; }
    :root { --bg: #0a0f14; --bg-2: #0f1720; --panel: rgba(17, 24, 39, 0.78); --panel-2: rgba(255, 255, 255, 0.03); --stroke: rgba(255, 255, 255, 0.08); --text: #e5e7eb; --muted: #94a3b8; --white: #ffffff; --primary: #19c37d; --primary-2: #14a86c; --glow: rgba(25, 195, 125, 0.18); --shadow: 0 20px 60px rgba(0, 0, 0, 0.35); --radius-xl: 26px; --radius-lg: 20px; --radius-md: 16px; --container: 1180px; }
    html { scroll-behavior: smooth; }
    body { font-family: Arial, Helvetica, sans-serif; color: var(--text); background: radial-gradient(circle at top left, rgba(25,195,125,0.16), transparent 30%), radial-gradient(circle at bottom right, rgba(25,195,125,0.10), transparent 28%), linear-gradient(180deg, #091018 0%, #0a0f14 100%); min-height: 100vh; line-height: 1.6; }
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
    @media (max-width: 640px) { .hero-main, .hero-side, .card, .legal-card { padding: 22px; } .hero { padding-top: 34px; } .section-title { font-size: 25px; } .hero h1 { font-size: 28px; } .topbar-inner { align-items: flex-start; } }`;

    const _CSS_PRO = `* { margin: 0; padding: 0; box-sizing: border-box; }
    :root { --bg: #ffffff; --bg-2: #f8fafc; --panel: #ffffff; --panel-2: #f1f5f9; --stroke: #e2e8f0; --text: #334155; --muted: #64748b; --white: #0f172a; --primary: #2563eb; --primary-2: #1d4ed8; --glow: rgba(37, 99, 235, 0.12); --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 6px 16px rgba(0,0,0,0.04); --radius-xl: 16px; --radius-lg: 12px; --radius-md: 8px; --container: 1100px; }
    html { scroll-behavior: smooth; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; color: var(--text); background: var(--bg); min-height: 100vh; line-height: 1.7; }
    a { color: var(--primary); text-decoration: none; transition: 0.2s ease; }
    a:hover { color: var(--primary-2); }
    .container { width: 100%; max-width: var(--container); margin: 0 auto; padding: 0 24px; }
    .topbar { position: sticky; top: 0; z-index: 100; background: rgba(255,255,255,0.92); backdrop-filter: blur(12px); border-bottom: 1px solid var(--stroke); }
    .topbar-inner { min-height: 70px; display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
    .logo-block { display: flex; flex-direction: column; gap: 2px; padding: 14px 0; }
    .logo-block strong { color: var(--white); font-size: 17px; line-height: 1.3; font-weight: 700; }
    .logo-block span { color: var(--muted); font-size: 13px; }
    .nav { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; padding: 12px 0; }
    .nav a { color: var(--text); font-size: 14px; padding: 8px 14px; border-radius: 8px; font-weight: 500; }
    .nav a:hover { background: var(--panel-2); color: var(--white); }
    .hero { position: relative; padding: 56px 0 32px; }
    .hero-grid { display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 28px; align-items: stretch; }
    .glass-card { background: var(--panel); border: 1px solid var(--stroke); box-shadow: var(--shadow); border-radius: var(--radius-xl); overflow: hidden; }
    .hero-main { padding: 40px; position: relative; }
    .hero-main::before { content: ""; position: absolute; inset: 0; background: linear-gradient(135deg, rgba(37,99,235,0.03), transparent 50%); pointer-events: none; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; border: 1px solid #bfdbfe; background: #eff6ff; color: #1e40af; border-radius: 999px; font-size: 13px; font-weight: 600; margin-bottom: 20px; }
    .hero h1 { font-size: 36px; line-height: 1.15; color: var(--white); max-width: 820px; margin-bottom: 16px; font-weight: 800; }
    .hero p { font-size: 16px; color: var(--muted); max-width: 760px; line-height: 1.7; }
    .hero-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 28px; }
    .btn { display: inline-flex; align-items: center; justify-content: center; min-height: 46px; padding: 0 22px; border-radius: 10px; font-weight: 600; font-size: 14px; border: 1px solid transparent; transition: 0.2s ease; cursor: pointer; }
    .btn-primary { background: var(--primary); color: #ffffff; box-shadow: 0 2px 8px var(--glow); }
    .btn-primary:hover { background: var(--primary-2); transform: translateY(-1px); box-shadow: 0 4px 12px var(--glow); }
    .btn-secondary { background: var(--panel); border-color: var(--stroke); color: var(--white); }
    .btn-secondary:hover { background: var(--panel-2); }
    .hero-side { padding: 28px; display: grid; gap: 14px; align-content: center; }
    .mini-stat { border-radius: 12px; border: 1px solid var(--stroke); background: var(--panel-2); padding: 16px 18px; }
    .mini-stat .label { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; margin-bottom: 6px; }
    .mini-stat .value { display: block; color: var(--white); font-size: 16px; font-weight: 700; line-height: 1.4; }
    section { padding: 28px 0; }
    .section-head { margin-bottom: 24px; }
    .section-title { color: var(--white); font-size: 26px; line-height: 1.2; margin-bottom: 8px; font-weight: 700; }
    .section-text { color: var(--muted); max-width: 850px; font-size: 15px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }
    .card { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); box-shadow: var(--shadow); padding: 24px; }
    .card h3 { color: var(--white); font-size: 18px; margin-bottom: 12px; font-weight: 700; }
    .card p { color: var(--muted); font-size: 15px; }
    .info-list { display: grid; gap: 10px; }
    .info-item { background: var(--panel-2); border: 1px solid var(--stroke); border-radius: 10px; padding: 14px 16px; }
    .info-item strong { display: block; color: var(--white); font-size: 13px; font-weight: 600; margin-bottom: 2px; }
    .info-item span { display: block; color: var(--text); font-size: 14px; word-break: break-word; }
    .card-large { grid-column: span 2; }
    .card-full { grid-column: 1 / -1; }
    .highlight-box { display: grid; gap: 10px; margin-top: 16px; }
    .highlight { display: flex; gap: 12px; align-items: flex-start; padding: 14px; border-radius: 10px; background: #eff6ff; border: 1px solid #bfdbfe; }
    .highlight-dot { width: 8px; height: 8px; border-radius: 999px; background: var(--primary); box-shadow: 0 0 0 4px rgba(37,99,235,0.12); margin-top: 7px; flex-shrink: 0; }
    .highlight div { color: var(--text); font-size: 14px; }
    .highlight div strong { color: var(--white); }
    .footer { margin-top: 40px; border-top: 1px solid var(--stroke); background: #f8fafc; }
    .footer-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; padding: 36px 0 20px; }
    .footer-col h4 { color: var(--white); font-size: 15px; font-weight: 700; margin-bottom: 12px; }
    .footer-col p, .footer-col a { color: var(--muted); font-size: 14px; }
    .footer-col a { display: block; margin-bottom: 8px; }
    .copyright { padding: 0 0 28px; color: #94a3b8; font-size: 13px; }
    .legal-wrapper { padding: 56px 0; }
    .legal-card { background: var(--panel); border: 1px solid var(--stroke); border-radius: 16px; box-shadow: var(--shadow); padding: 36px; }
    .legal-card h1 { color: var(--white); font-size: 32px; margin-bottom: 10px; font-weight: 800; }
    .legal-card p { color: var(--text); font-size: 15px; margin-bottom: 16px; }
    .legal-card h2 { color: var(--white); font-size: 18px; font-weight: 700; margin: 24px 0 10px; }
    .top-space { margin-top: 12px; }
    @media (max-width: 980px) { .hero-grid, .grid-3, .grid-2, .footer-grid { grid-template-columns: 1fr; } .card-large, .card-full { grid-column: auto; } .hero h1 { font-size: 28px; } }
    @media (max-width: 640px) { .hero-main, .hero-side, .card, .legal-card { padding: 22px; } .hero { padding-top: 32px; } .section-title { font-size: 22px; } .hero h1 { font-size: 24px; } .topbar-inner { align-items: flex-start; } }`;

    const _CSS_NEXTGEN = `* { margin: 0; padding: 0; box-sizing: border-box; }
    :root { --bg: #0c0a1a; --bg-2: #110e24; --panel: rgba(22, 18, 48, 0.82); --panel-2: rgba(139, 92, 246, 0.04); --stroke: rgba(139, 92, 246, 0.12); --text: #cbd5e1; --muted: #94a3b8; --white: #f1f5f9; --primary: #a78bfa; --primary-2: #8b5cf6; --accent: #c084fc; --glow: rgba(139, 92, 246, 0.20); --shadow: 0 8px 32px rgba(0, 0, 0, 0.40), 0 2px 8px rgba(139, 92, 246, 0.06); --radius-xl: 24px; --radius-lg: 18px; --radius-md: 14px; --container: 1140px; }
    @keyframes shimmer { 0%,100% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } }
    html { scroll-behavior: smooth; }
    body { font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: var(--text); background: var(--bg); min-height: 100vh; line-height: 1.7; position: relative; }
    body::before { content: ""; position: fixed; inset: 0; background: radial-gradient(ellipse 80% 50% at 20% 10%, rgba(139,92,246,0.13), transparent 55%), radial-gradient(ellipse 60% 40% at 80% 85%, rgba(168,85,247,0.09), transparent 50%), radial-gradient(ellipse 50% 50% at 50% 50%, rgba(99,102,241,0.06), transparent 60%); pointer-events: none; z-index: 0; }
    body > * { position: relative; z-index: 1; }
    a { color: var(--primary); text-decoration: none; transition: color 0.25s ease; }
    a:hover { color: var(--accent); }
    .container { width: 100%; max-width: var(--container); margin: 0 auto; padding: 0 22px; }
    .topbar { position: sticky; top: 0; z-index: 100; background: rgba(12, 10, 26, 0.80); backdrop-filter: blur(20px) saturate(1.4); border-bottom: 1px solid rgba(139,92,246,0.08); }
    .topbar-inner { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
    .logo-block { display: flex; flex-direction: column; gap: 3px; padding: 14px 0; }
    .logo-block strong { color: var(--white); font-size: 17px; line-height: 1.25; font-weight: 700; letter-spacing: -0.01em; }
    .logo-block span { color: var(--muted); font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; }
    .nav { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; padding: 12px 0; }
    .nav a { color: var(--muted); font-size: 13px; padding: 8px 14px; border-radius: 10px; font-weight: 500; transition: all 0.25s ease; }
    .nav a:hover { background: rgba(139,92,246,0.08); color: var(--white); }
    .hero { position: relative; padding: 64px 0 28px; }
    .hero-grid { display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 22px; align-items: stretch; }
    .glass-card { background: var(--panel); border: 1px solid var(--stroke); box-shadow: var(--shadow); border-radius: var(--radius-xl); overflow: hidden; backdrop-filter: blur(8px); }
    .hero-main { padding: 36px; position: relative; }
    .hero-main::before { content: ""; position: absolute; inset: 0; background: linear-gradient(135deg, rgba(139,92,246,0.08), transparent 40%), linear-gradient(225deg, rgba(168,85,247,0.04), transparent 50%); pointer-events: none; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; border: 1px solid rgba(139,92,246,0.25); background: rgba(139,92,246,0.10); color: #c4b5fd; border-radius: 999px; font-size: 12px; font-weight: 600; margin-bottom: 20px; letter-spacing: 0.02em; text-transform: uppercase; }
    .hero h1 { font-size: 40px; line-height: 1.1; color: var(--white); max-width: 820px; margin-bottom: 14px; font-weight: 800; letter-spacing: -0.025em; background: linear-gradient(135deg, #f1f5f9, #c4b5fd); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
    .hero p { font-size: 16px; color: var(--muted); max-width: 760px; line-height: 1.7; }
    .hero-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 28px; }
    .btn { display: inline-flex; align-items: center; justify-content: center; min-height: 48px; padding: 0 22px; border-radius: 14px; font-weight: 600; font-size: 14px; border: 1px solid transparent; transition: all 0.3s ease; cursor: pointer; }
    .btn-primary { background: linear-gradient(135deg, #8b5cf6, #a78bfa); color: #ffffff; box-shadow: 0 4px 16px var(--glow), inset 0 1px 0 rgba(255,255,255,0.10); }
    .btn-primary:hover { background: linear-gradient(135deg, #7c3aed, #8b5cf6); transform: translateY(-2px); box-shadow: 0 8px 24px var(--glow), inset 0 1px 0 rgba(255,255,255,0.12); }
    .btn-secondary { background: rgba(139,92,246,0.06); border-color: rgba(139,92,246,0.18); color: var(--white); backdrop-filter: blur(4px); }
    .btn-secondary:hover { background: rgba(139,92,246,0.12); border-color: rgba(139,92,246,0.28); }
    .hero-side { padding: 26px; display: grid; gap: 12px; align-content: center; }
    .mini-stat { border-radius: 14px; border: 1px solid rgba(139,92,246,0.10); background: rgba(139,92,246,0.04); padding: 16px 18px; transition: border-color 0.3s ease, background 0.3s ease; }
    .mini-stat:hover { border-color: rgba(139,92,246,0.22); background: rgba(139,92,246,0.08); }
    .mini-stat .label { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; margin-bottom: 6px; }
    .mini-stat .value { display: block; color: var(--white); font-size: 17px; font-weight: 700; line-height: 1.4; }
    section { padding: 26px 0; }
    .section-head { margin-bottom: 22px; }
    .section-title { color: var(--white); font-size: 28px; line-height: 1.15; margin-bottom: 8px; font-weight: 700; letter-spacing: -0.02em; }
    .section-text { color: var(--muted); max-width: 850px; font-size: 15px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px; }
    .card { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); box-shadow: var(--shadow); padding: 24px; backdrop-filter: blur(6px); transition: border-color 0.3s ease, transform 0.3s ease; }
    .card:hover { border-color: rgba(139,92,246,0.22); transform: translateY(-2px); }
    .card h3 { color: var(--white); font-size: 18px; margin-bottom: 12px; font-weight: 700; }
    .card p { color: var(--muted); font-size: 15px; line-height: 1.7; }
    .info-list { display: grid; gap: 10px; }
    .info-item { background: rgba(139,92,246,0.03); border: 1px solid rgba(139,92,246,0.08); border-radius: 12px; padding: 14px 16px; transition: background 0.25s ease; }
    .info-item:hover { background: rgba(139,92,246,0.06); }
    .info-item strong { display: block; color: var(--primary); font-size: 11px; font-weight: 600; margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.06em; }
    .info-item span { display: block; color: var(--text); font-size: 14px; word-break: break-word; }
    .card-large { grid-column: span 2; }
    .card-full { grid-column: 1 / -1; }
    .highlight-box { display: grid; gap: 10px; margin-top: 16px; }
    .highlight { display: flex; gap: 12px; align-items: flex-start; padding: 14px 16px; border-radius: 12px; background: rgba(139,92,246,0.06); border: 1px solid rgba(139,92,246,0.14); transition: background 0.25s ease; }
    .highlight:hover { background: rgba(139,92,246,0.10); }
    .highlight-dot { width: 8px; height: 8px; border-radius: 999px; background: linear-gradient(135deg, #8b5cf6, #a78bfa); box-shadow: 0 0 0 4px rgba(139,92,246,0.14); margin-top: 7px; flex-shrink: 0; }
    .highlight div { color: var(--text); font-size: 14px; }
    .highlight div strong { color: var(--white); }
    .footer { margin-top: 36px; border-top: 1px solid rgba(139,92,246,0.08); background: rgba(12, 10, 26, 0.60); }
    .footer-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; padding: 34px 0 20px; }
    .footer-col h4 { color: var(--white); font-size: 15px; font-weight: 700; margin-bottom: 12px; }
    .footer-col p, .footer-col a { color: var(--muted); font-size: 14px; }
    .footer-col a { display: block; margin-bottom: 8px; transition: color 0.25s ease; }
    .footer-col a:hover { color: var(--primary); }
    .copyright { padding: 0 0 28px; color: #64748b; font-size: 12px; letter-spacing: 0.02em; }
    .legal-wrapper { padding: 60px 0; }
    .legal-card { background: var(--panel); border: 1px solid var(--stroke); border-radius: 20px; box-shadow: var(--shadow); padding: 36px; backdrop-filter: blur(6px); }
    .legal-card h1 { color: var(--white); font-size: 32px; margin-bottom: 10px; font-weight: 800; letter-spacing: -0.02em; }
    .legal-card p { color: var(--text); font-size: 15px; margin-bottom: 16px; line-height: 1.7; }
    .legal-card h2 { color: var(--white); font-size: 18px; font-weight: 700; margin: 24px 0 10px; }
    .top-space { margin-top: 12px; }
    @media (max-width: 980px) { .hero-grid, .grid-3, .grid-2, .footer-grid { grid-template-columns: 1fr; } .card-large, .card-full { grid-column: auto; } .hero h1 { font-size: 30px; } }
    @media (max-width: 640px) { .hero-main, .hero-side, .card, .legal-card { padding: 22px; } .hero { padding-top: 34px; } .section-title { font-size: 22px; } .hero h1 { font-size: 26px; } .topbar-inner { align-items: flex-start; } }`;

    const _CSS_PREMIUM = `* { margin: 0; padding: 0; box-sizing: border-box; }
    :root { --bg: #0f1117; --bg-2: #161922; --panel: rgba(26, 28, 38, 0.90); --panel-2: rgba(212, 149, 107, 0.04); --stroke: rgba(212, 149, 107, 0.10); --text: #c8c2ba; --muted: #8a8580; --white: #faf6f1; --primary: #d4956b; --primary-2: #c4854e; --accent: #e8a87c; --glow: rgba(212, 149, 107, 0.18); --shadow: 0 8px 32px rgba(0,0,0,0.45), 0 2px 6px rgba(212,149,107,0.04); --radius-xl: 20px; --radius-lg: 16px; --radius-md: 12px; --container: 1120px; }
    @keyframes warmPulse { 0%,100% { opacity: 0.6; } 50% { opacity: 1; } }
    @keyframes subtleDrift { 0%,100% { transform: translate(0,0); } 50% { transform: translate(12px,-8px); } }
    html { scroll-behavior: smooth; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; color: var(--text); background: var(--bg); min-height: 100vh; line-height: 1.7; position: relative; overflow-x: hidden; }
    body::before { content: ""; position: fixed; inset: 0; background: radial-gradient(ellipse 70% 50% at 15% 5%, rgba(212,149,107,0.08), transparent 50%), radial-gradient(ellipse 50% 40% at 85% 90%, rgba(196,133,78,0.06), transparent 50%), radial-gradient(ellipse 60% 60% at 50% 40%, rgba(232,168,124,0.03), transparent 55%); pointer-events: none; z-index: 0; animation: subtleDrift 30s ease-in-out infinite; }
    body::after { content: ""; position: fixed; top: -50%; left: -50%; width: 200%; height: 200%; background: repeating-conic-gradient(rgba(212,149,107,0.012) 0deg, transparent 1deg, transparent 90deg); pointer-events: none; z-index: 0; }
    body > * { position: relative; z-index: 1; }
    a { color: var(--primary); text-decoration: none; transition: color 0.3s ease; }
    a:hover { color: var(--accent); }
    .container { width: 100%; max-width: var(--container); margin: 0 auto; padding: 0 22px; }
    .topbar { position: sticky; top: 0; z-index: 100; background: rgba(15,17,23,0.85); backdrop-filter: blur(24px) saturate(1.3); border-bottom: 1px solid rgba(212,149,107,0.06); }
    .topbar-inner { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
    .logo-block { display: flex; flex-direction: column; gap: 3px; padding: 14px 0; }
    .logo-block strong { color: var(--white); font-size: 17px; line-height: 1.25; font-weight: 700; letter-spacing: -0.01em; }
    .logo-block span { color: var(--muted); font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; }
    .nav { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; padding: 12px 0; }
    .nav a { color: var(--muted); font-size: 13px; padding: 8px 14px; border-radius: 10px; font-weight: 500; transition: all 0.3s ease; }
    .nav a:hover { background: rgba(212,149,107,0.08); color: var(--white); }
    .hero { position: relative; padding: 60px 0 28px; }
    .hero-grid { display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 22px; align-items: stretch; }
    .glass-card { background: var(--panel); border: 1px solid var(--stroke); box-shadow: var(--shadow); border-radius: var(--radius-xl); overflow: hidden; backdrop-filter: blur(10px); position: relative; }
    .glass-card::before { content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, transparent, var(--primary), var(--accent), transparent); opacity: 0.6; }
    .hero-main { padding: 36px; position: relative; }
    .hero-main::before { content: ""; position: absolute; inset: 0; background: linear-gradient(145deg, rgba(212,149,107,0.06), transparent 35%), linear-gradient(225deg, rgba(232,168,124,0.03), transparent 50%); pointer-events: none; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; border: 1px solid rgba(212,149,107,0.22); background: rgba(212,149,107,0.08); color: var(--accent); border-radius: 999px; font-size: 12px; font-weight: 600; margin-bottom: 20px; letter-spacing: 0.03em; text-transform: uppercase; }
    .hero h1 { font-size: 40px; line-height: 1.08; color: var(--white); max-width: 820px; margin-bottom: 14px; font-weight: 800; letter-spacing: -0.03em; }
    .hero p { font-size: 16px; color: var(--muted); max-width: 760px; line-height: 1.7; }
    .hero-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 28px; }
    .btn { display: inline-flex; align-items: center; justify-content: center; min-height: 48px; padding: 0 24px; border-radius: 12px; font-weight: 600; font-size: 14px; border: 1px solid transparent; transition: all 0.3s ease; cursor: pointer; }
    .btn-primary { background: linear-gradient(135deg, #c4854e, #d4956b); color: #1a1207; box-shadow: 0 4px 18px var(--glow); }
    .btn-primary:hover { background: linear-gradient(135deg, #b3763f, #c4854e); transform: translateY(-2px); box-shadow: 0 8px 28px rgba(212,149,107,0.25); }
    .btn-secondary { background: rgba(212,149,107,0.06); border-color: rgba(212,149,107,0.15); color: var(--white); }
    .btn-secondary:hover { background: rgba(212,149,107,0.12); border-color: rgba(212,149,107,0.25); }
    .hero-side { padding: 26px; display: grid; gap: 12px; align-content: center; }
    .mini-stat { border-radius: 14px; border: 1px solid rgba(212,149,107,0.08); background: rgba(212,149,107,0.03); padding: 16px 18px; transition: all 0.3s ease; position: relative; overflow: hidden; }
    .mini-stat::after { content: ""; position: absolute; top: 0; right: 0; width: 40px; height: 100%; background: linear-gradient(90deg, transparent, rgba(212,149,107,0.05)); pointer-events: none; }
    .mini-stat:hover { border-color: rgba(212,149,107,0.20); background: rgba(212,149,107,0.06); transform: translateX(3px); }
    .mini-stat .label { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; margin-bottom: 6px; }
    .mini-stat .value { display: block; color: var(--white); font-size: 17px; font-weight: 700; line-height: 1.4; }
    section { padding: 28px 0; }
    .section-head { margin-bottom: 24px; position: relative; padding-left: 16px; }
    .section-head::before { content: ""; position: absolute; left: 0; top: 4px; width: 3px; height: 28px; background: linear-gradient(180deg, var(--primary), var(--accent)); border-radius: 4px; }
    .section-title { color: var(--white); font-size: 28px; line-height: 1.15; margin-bottom: 8px; font-weight: 700; letter-spacing: -0.02em; }
    .section-text { color: var(--muted); max-width: 850px; font-size: 15px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px; }
    .card { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); box-shadow: var(--shadow); padding: 24px; backdrop-filter: blur(6px); transition: all 0.35s ease; position: relative; overflow: hidden; }
    .card::after { content: ""; position: absolute; inset: 0; border-radius: inherit; background: linear-gradient(135deg, rgba(212,149,107,0.04), transparent 60%); opacity: 0; transition: opacity 0.35s ease; pointer-events: none; }
    .card:hover { border-color: rgba(212,149,107,0.20); transform: translateY(-3px); box-shadow: 0 12px 40px rgba(0,0,0,0.50), 0 4px 12px rgba(212,149,107,0.06); }
    .card:hover::after { opacity: 1; }
    .card h3 { color: var(--white); font-size: 18px; margin-bottom: 12px; font-weight: 700; }
    .card p { color: var(--muted); font-size: 15px; line-height: 1.7; }
    .info-list { display: grid; gap: 10px; }
    .info-item { background: rgba(212,149,107,0.03); border: 1px solid rgba(212,149,107,0.07); border-radius: 12px; padding: 14px 16px; transition: all 0.25s ease; }
    .info-item:hover { background: rgba(212,149,107,0.06); border-color: rgba(212,149,107,0.14); }
    .info-item strong { display: block; color: var(--primary); font-size: 11px; font-weight: 600; margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.06em; }
    .info-item span { display: block; color: var(--text); font-size: 14px; word-break: break-word; }
    .card-large { grid-column: span 2; }
    .card-full { grid-column: 1 / -1; }
    .highlight-box { display: grid; gap: 10px; margin-top: 16px; }
    .highlight { display: flex; gap: 12px; align-items: flex-start; padding: 14px 16px; border-radius: 12px; background: rgba(212,149,107,0.05); border: 1px solid rgba(212,149,107,0.10); transition: all 0.25s ease; }
    .highlight:hover { background: rgba(212,149,107,0.09); border-color: rgba(212,149,107,0.18); }
    .highlight-dot { width: 8px; height: 8px; border-radius: 999px; background: linear-gradient(135deg, #c4854e, #d4956b); box-shadow: 0 0 0 4px rgba(212,149,107,0.12); margin-top: 7px; flex-shrink: 0; animation: warmPulse 3s ease-in-out infinite; }
    .highlight div { color: var(--text); font-size: 14px; }
    .highlight div strong { color: var(--white); }
    .footer { margin-top: 36px; border-top: 1px solid rgba(212,149,107,0.08); background: rgba(15,17,23,0.70); }
    .footer-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; padding: 34px 0 20px; }
    .footer-col h4 { color: var(--white); font-size: 15px; font-weight: 700; margin-bottom: 12px; }
    .footer-col p, .footer-col a { color: var(--muted); font-size: 14px; }
    .footer-col a { display: block; margin-bottom: 8px; transition: color 0.3s ease; }
    .footer-col a:hover { color: var(--primary); }
    .copyright { padding: 0 0 28px; color: #6b6560; font-size: 12px; letter-spacing: 0.02em; }
    .legal-wrapper { padding: 60px 0; }
    .legal-card { background: var(--panel); border: 1px solid var(--stroke); border-radius: 20px; box-shadow: var(--shadow); padding: 36px; backdrop-filter: blur(6px); }
    .legal-card h1 { color: var(--white); font-size: 32px; margin-bottom: 10px; font-weight: 800; letter-spacing: -0.02em; }
    .legal-card p { color: var(--text); font-size: 15px; margin-bottom: 16px; line-height: 1.7; }
    .legal-card h2 { color: var(--white); font-size: 18px; font-weight: 700; margin: 24px 0 10px; }
    .top-space { margin-top: 12px; }
    @media (max-width: 980px) { .hero-grid, .grid-3, .grid-2, .footer-grid { grid-template-columns: 1fr; } .card-large, .card-full { grid-column: auto; } .hero h1 { font-size: 30px; } .section-head { padding-left: 14px; } }
    @media (max-width: 640px) { .hero-main, .hero-side, .card, .legal-card { padding: 22px; } .hero { padding-top: 34px; } .section-title { font-size: 22px; } .hero h1 { font-size: 26px; } .topbar-inner { align-items: flex-start; } .section-head::before { height: 22px; } }`;

    const _CSS_CORPORATE = `* { margin: 0; padding: 0; box-sizing: border-box; }
    :root { --bg: #f7f5f0; --bg-2: #ffffff; --panel: #ffffff; --panel-2: #f0ede7; --stroke: #e2ded6; --text: #3a4055; --muted: #7c8298; --white: #1c2639; --primary: #0d7c66; --primary-2: #0a6553; --accent: #c99a2e; --glow: rgba(13,124,102,0.10); --shadow: 0 2px 16px rgba(28,38,57,0.07); --radius-xl: 16px; --radius-lg: 12px; --radius-md: 8px; --container: 1140px; }
    html { scroll-behavior: smooth; }
    body { font-family: "DM Sans", system-ui, -apple-system, sans-serif; color: var(--text); background: var(--bg); min-height: 100vh; line-height: 1.7; }
    a { color: var(--primary); text-decoration: none; transition: color 0.2s ease; }
    a:hover { color: var(--primary-2); }
    .container { width: 100%; max-width: var(--container); margin: 0 auto; padding: 0 22px; }
    .topbar { position: sticky; top: 0; z-index: 100; background: rgba(255,255,255,0.92); backdrop-filter: blur(12px); border-bottom: 1px solid var(--stroke); }
    .topbar-inner { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
    .logo-block { display: flex; flex-direction: column; gap: 3px; padding: 14px 0; }
    .logo-block strong { color: var(--white); font-size: 17px; line-height: 1.25; font-weight: 700; }
    .logo-block span { color: var(--primary); font-size: 12px; letter-spacing: 0.05em; font-weight: 600; }
    .nav { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; padding: 12px 0; }
    .nav a { color: var(--muted); font-size: 13px; padding: 8px 14px; border-radius: 8px; font-weight: 500; transition: all 0.2s ease; }
    .nav a:hover { background: rgba(13,124,102,0.06); color: var(--primary); }
    .hero { position: relative; padding: 48px 0 20px; background: linear-gradient(180deg, #efece6 0%, var(--bg) 100%); }
    .hero-grid { display: grid; grid-template-columns: 1.3fr 0.7fr; gap: 24px; align-items: stretch; }
    .glass-card { background: var(--panel); border: 1px solid var(--stroke); box-shadow: var(--shadow); border-radius: var(--radius-xl); overflow: hidden; }
    .hero-main { padding: 36px; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 7px 16px; border: 1px solid rgba(13,124,102,0.20); background: rgba(13,124,102,0.06); color: var(--primary); border-radius: 999px; font-size: 12px; font-weight: 600; margin-bottom: 18px; letter-spacing: 0.02em; }
    .hero h1 { font-size: 38px; line-height: 1.1; color: var(--white); max-width: 820px; margin-bottom: 14px; font-weight: 800; letter-spacing: -0.03em; }
    .hero p { font-size: 15px; color: var(--muted); max-width: 700px; line-height: 1.75; }
    .hero-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 24px; }
    .btn { display: inline-flex; align-items: center; justify-content: center; min-height: 46px; padding: 0 24px; border-radius: 10px; font-weight: 600; font-size: 14px; border: 1px solid transparent; transition: all 0.2s ease; cursor: pointer; }
    .btn-primary { background: var(--primary); color: #fff; box-shadow: 0 2px 8px var(--glow); }
    .btn-primary:hover { background: var(--primary-2); transform: translateY(-1px); box-shadow: 0 4px 16px rgba(13,124,102,0.18); }
    .btn-secondary { background: transparent; border-color: var(--stroke); color: var(--white); }
    .btn-secondary:hover { background: var(--panel-2); border-color: var(--primary); color: var(--primary); }
    .hero-side { padding: 24px; display: grid; gap: 10px; align-content: center; }
    .mini-stat { border-radius: 10px; border: 1px solid var(--stroke); background: var(--panel); padding: 14px 16px; transition: all 0.2s ease; border-left: 3px solid var(--primary); }
    .mini-stat:hover { border-left-color: var(--accent); box-shadow: 0 4px 12px rgba(28,38,57,0.08); transform: translateX(2px); }
    .mini-stat .label { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; margin-bottom: 4px; }
    .mini-stat .value { display: block; color: var(--white); font-size: 15px; font-weight: 700; line-height: 1.4; }
    section { padding: 32px 0; }
    .section-head { margin-bottom: 28px; padding-left: 18px; position: relative; }
    .section-head::before { content: ""; position: absolute; left: 0; top: 2px; width: 4px; height: 32px; background: linear-gradient(180deg, var(--primary), var(--accent)); border-radius: 4px; }
    .section-title { color: var(--white); font-size: 28px; line-height: 1.15; margin-bottom: 8px; font-weight: 800; letter-spacing: -0.02em; }
    .section-text { color: var(--muted); max-width: 800px; font-size: 15px; line-height: 1.7; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px; }
    .card { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); box-shadow: var(--shadow); padding: 24px; transition: all 0.25s ease; border-top: 3px solid transparent; }
    .card:hover { border-top-color: var(--primary); box-shadow: 0 6px 24px rgba(28,38,57,0.10); transform: translateY(-2px); }
    .card h3 { color: var(--white); font-size: 17px; margin-bottom: 10px; font-weight: 700; }
    .card p { color: var(--text); font-size: 14px; line-height: 1.7; }
    .info-list { display: grid; gap: 8px; }
    .info-item { background: var(--panel-2); border: 1px solid rgba(226,222,214,0.6); border-radius: 10px; padding: 14px 16px; transition: all 0.2s ease; }
    .info-item:hover { background: rgba(13,124,102,0.04); border-color: rgba(13,124,102,0.15); }
    .info-item strong { display: block; color: var(--primary); font-size: 11px; font-weight: 700; margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.06em; }
    .info-item span { display: block; color: var(--white); font-size: 14px; word-break: break-word; font-weight: 500; }
    .card-large { grid-column: span 2; }
    .card-full { grid-column: 1 / -1; }
    .highlight-box { display: grid; gap: 8px; margin-top: 16px; }
    .highlight { display: flex; gap: 12px; align-items: flex-start; padding: 12px 16px; border-radius: 10px; background: rgba(13,124,102,0.04); border: 1px solid rgba(13,124,102,0.10); transition: all 0.2s ease; }
    .highlight:hover { background: rgba(13,124,102,0.08); border-color: rgba(13,124,102,0.18); }
    .highlight-dot { width: 8px; height: 8px; border-radius: 999px; background: var(--accent); box-shadow: 0 0 0 3px rgba(201,154,46,0.15); margin-top: 7px; flex-shrink: 0; }
    .highlight div { color: var(--text); font-size: 14px; }
    .highlight div strong { color: var(--white); }
    .footer { margin-top: 40px; border-top: none; background: var(--white); color: #c8cad4; }
    .footer a { color: #a0a4b4; }
    .footer a:hover { color: #fff; }
    .footer-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; padding: 40px 0 20px; }
    .footer-col h4 { color: #fff; font-size: 14px; font-weight: 700; margin-bottom: 14px; text-transform: uppercase; letter-spacing: 0.04em; }
    .footer-col p, .footer-col a { color: #a0a4b4; font-size: 14px; }
    .footer-col a { display: block; margin-bottom: 8px; }
    .copyright { padding: 0 0 28px; color: #6b7084; font-size: 12px; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 20px; margin-top: 8px; }
    .legal-wrapper { padding: 60px 0; }
    .legal-card { background: var(--panel); border: 1px solid var(--stroke); border-radius: 16px; box-shadow: var(--shadow); padding: 40px; }
    .legal-card h1 { color: var(--white); font-size: 32px; margin-bottom: 10px; font-weight: 800; letter-spacing: -0.02em; }
    .legal-card p { color: var(--text); font-size: 15px; margin-bottom: 16px; line-height: 1.7; }
    .legal-card h2 { color: var(--white); font-size: 18px; font-weight: 700; margin: 28px 0 10px; }
    .top-space { margin-top: 12px; }
    .corp-kpi { padding: 0 0 16px; }
    .corp-kpi-bar { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); padding: 20px 24px; box-shadow: var(--shadow); }
    .corp-kpi-item { text-align: center; padding: 8px 4px; border-right: 1px solid var(--stroke); }
    .corp-kpi-item:last-child { border-right: none; }
    .corp-kpi-label { display: block; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; margin-bottom: 6px; }
    .corp-kpi-val { display: block; font-size: 15px; color: var(--white); font-weight: 700; }
    .corp-profile { display: grid; grid-template-columns: 1fr 340px; gap: 28px; align-items: start; }
    .corp-profile-main { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); padding: 32px; box-shadow: var(--shadow); }
    .corp-profile-main h3 { color: var(--white); font-size: 20px; font-weight: 700; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 2px solid rgba(13,124,102,0.15); }
    .corp-profile-main p { color: var(--text); font-size: 15px; line-height: 1.8; margin-bottom: 14px; }
    .corp-profile-main p:last-child { margin-bottom: 0; }
    .corp-profile-aside { position: sticky; top: 90px; }
    .corp-aside-card { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); padding: 24px; box-shadow: var(--shadow); border-top: 3px solid var(--accent); }
    .corp-aside-card h4 { color: var(--white); font-size: 15px; font-weight: 700; margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.03em; }
    .corp-aside-card dl { display: grid; gap: 12px; }
    .corp-aside-card dt { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
    .corp-aside-card dd { font-size: 14px; color: var(--white); font-weight: 600; margin: 2px 0 0; word-break: break-word; }
    .corp-desc { color: var(--text); font-size: 15px; line-height: 1.8; margin-bottom: 12px; max-width: 900px; }
    .corp-reg-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
    .corp-reg-card { background: var(--panel); border: 1px solid var(--stroke); border-left: 3px solid var(--primary); border-radius: 0 var(--radius-md) var(--radius-md) 0; padding: 18px 20px; box-shadow: var(--shadow); transition: all 0.2s ease; }
    .corp-reg-card:hover { border-left-color: var(--accent); box-shadow: 0 4px 16px rgba(28,38,57,0.10); }
    .corp-reg-card .corp-reg-label { display: block; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; margin-bottom: 6px; }
    .corp-reg-card .corp-reg-value { display: block; font-size: 14px; color: var(--white); font-weight: 600; word-break: break-word; }
    .corp-activity { background: var(--panel); border: 1px solid var(--stroke); border-radius: var(--radius-lg); padding: 28px; box-shadow: var(--shadow); }
    .corp-activity h3 { color: var(--white); font-size: 18px; font-weight: 700; margin-bottom: 8px; }
    .corp-activity-main { font-size: 15px; color: var(--primary); font-weight: 600; margin-bottom: 8px; padding: 14px 18px; background: rgba(13,124,102,0.05); border: 1px solid rgba(13,124,102,0.12); border-radius: 10px; }
    .corp-activity-list { list-style: none; padding: 0; margin: 16px 0 0; }
    .corp-activity-list li { padding: 10px 16px; border-left: 2px solid var(--stroke); margin-bottom: 6px; color: var(--text); font-size: 14px; transition: border-color 0.2s; }
    .corp-activity-list li:hover { border-left-color: var(--primary); }
    @media (max-width: 980px) { .hero-grid, .grid-3, .grid-2, .footer-grid, .corp-reg-grid { grid-template-columns: 1fr; } .card-large, .card-full { grid-column: auto; } .hero h1 { font-size: 30px; } .corp-profile { grid-template-columns: 1fr; } .corp-kpi-bar { grid-template-columns: repeat(3, 1fr); } .corp-profile-aside { position: static; } }
    @media (max-width: 640px) { .hero-main, .hero-side, .card, .legal-card, .corp-profile-main { padding: 22px; } .hero { padding-top: 28px; } .section-title { font-size: 22px; } .hero h1 { font-size: 26px; } .topbar-inner { align-items: flex-start; } .corp-kpi-bar { grid-template-columns: repeat(2, 1fr); } .corp-kpi-item { border-right: none; border-bottom: 1px solid var(--stroke); padding-bottom: 10px; } .corp-kpi-item:last-child { border-bottom: none; } .corp-reg-grid { gap: 10px; } }`;

    const _tmplOverride = url.searchParams.get("__template");
    const _tmplId = _tmplOverride || EMBEDDED_TEMPLATE;
    const _cssMap = { "classic": _CSS_CLASSIC, "professional": _CSS_PRO, "nextgen": _CSS_NEXTGEN, "premium": _CSS_PREMIUM, "corporate": _CSS_CORPORATE };
    const _activeCSS = _cssMap[_tmplId] || _CSS_CLASSIC;

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
  ${_tmplId === "nextgen" ? '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">' : ''}
  ${_tmplId === "corporate" ? '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">' : ''}
  <style>${_activeCSS}</style>
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
                ${toTelHref(empresa.contato.telefone) ? `<a href="${escapeHtml(toTelHref(empresa.contato.telefone))}">${escapeHtml(formatTelefoneBr(empresa.contato.telefone))}</a>` : `<span>${escapeHtml(formatTelefoneBr(empresa.contato.telefone))}</span>`}
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

    const corpAtividadesHtml = empresa.atividadesSecundarias && empresa.atividadesSecundarias.length
        ? `<ul class="corp-activity-list">` + empresa.atividadesSecundarias.map((a) => `<li>${escapeHtml(a)}</li>`).join("") + `</ul>`
        : `<p style="color: var(--muted); font-size: 14px; margin-top: 10px;">Nenhuma atividade secundária registrada.</p>`;

    function corporateHome() {
      const ni = "Não informado";
      const mf = empresa.matrizFilial || "Matriz";
      const ds = empresa.dataSituacao || ni;
      const ms = empresa.motivoSituacao || ni;
      const ef = empresa.efr || ni;
      const se = empresa.situacaoEspecial || ni;
      const dse = empresa.dataSituacaoEspecial || ni;
      return `
      ${header()}
      <main>
        <section class="hero">
          <div class="container">
            <div class="hero-grid">
              <div class="glass-card hero-main">
                <div class="badge">Situação: ${escapeHtml(empresa.situacao)} · ${escapeHtml(mf)} · ${escapeHtml(empresa.endereco.cidade)} / ${escapeHtml(empresa.endereco.uf)}</div>
                <h1>${escapeHtml(empresa.razaoSocial)}</h1>
                <p>Empresa constituída em ${escapeHtml(empresa.abertura)}, inscrita sob o CNPJ ${escapeHtml(empresa.cnpj)}, com sede em ${escapeHtml(empresa.endereco.cidade)}/${escapeHtml(empresa.endereco.uf)}. Atuante no segmento de ${escapeHtml(empresa.cnaePrincipal)}, a empresa mantém registro regular junto à Receita Federal do Brasil, operando sob a natureza jurídica de ${escapeHtml(empresa.naturezaJuridica)} com capital social de ${escapeHtml(empresa.capitalSocial)}.</p>
                <div class="hero-actions">
                  <a href="#dados" class="btn btn-primary">Registro Federal completo</a>
                  <a href="#atividades" class="btn btn-secondary">Atividade econômica</a>
                  <a href="#contato" class="btn btn-secondary">Contato</a>
                </div>
              </div>
              <div class="glass-card hero-side">
                <div class="mini-stat"><span class="label">CNPJ</span><span class="value">${escapeHtml(empresa.cnpj)}</span></div>
                <div class="mini-stat"><span class="label">Situação Cadastral</span><span class="value">${escapeHtml(empresa.situacao)}${ds !== ni ? ` desde ${escapeHtml(ds)}` : ""}</span></div>
                <div class="mini-stat"><span class="label">Tipo de Estabelecimento</span><span class="value">${escapeHtml(mf)}</span></div>
                <div class="mini-stat"><span class="label">Data de Abertura</span><span class="value">${escapeHtml(empresa.abertura)}</span></div>
                <div class="mini-stat"><span class="label">Capital Social</span><span class="value">${escapeHtml(empresa.capitalSocial)}</span></div>
              </div>
            </div>
          </div>
        </section>
        <section class="corp-kpi">
          <div class="container">
            <div class="corp-kpi-bar">
              <div class="corp-kpi-item"><span class="corp-kpi-label">Natureza Jurídica</span><span class="corp-kpi-val">${escapeHtml(empresa.naturezaJuridica)}</span></div>
              <div class="corp-kpi-item"><span class="corp-kpi-label">Porte</span><span class="corp-kpi-val">${escapeHtml(empresa.porte)}</span></div>
              <div class="corp-kpi-item"><span class="corp-kpi-label">Optante MEI</span><span class="corp-kpi-val">${escapeHtml(empresa.mei)}</span></div>
              <div class="corp-kpi-item"><span class="corp-kpi-label">Abertura</span><span class="corp-kpi-val">${escapeHtml(empresa.abertura)}</span></div>
              <div class="corp-kpi-item"><span class="corp-kpi-label">UF</span><span class="corp-kpi-val">${escapeHtml(empresa.endereco.uf)}</span></div>
            </div>
          </div>
        </section>
        <section id="quem-somos">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Perfil Empresarial</h2><p class="section-text">Informações detalhadas sobre a constituição, atuação e estrutura organizacional da empresa, disponibilizadas para consulta pública e verificação cadastral.</p></div>
            <div class="corp-profile">
              <div class="corp-profile-main">
                <h3>Sobre a Empresa</h3>
                <p><strong>${escapeHtml(empresa.razaoSocial)}</strong>${empresa.nomeFantasia !== ni ? `, operando sob o nome fantasia <strong>${escapeHtml(empresa.nomeFantasia)}</strong>,` : ""} é uma empresa de natureza jurídica <strong>${escapeHtml(empresa.naturezaJuridica)}</strong>, enquadrada como <strong>${escapeHtml(empresa.porte)}</strong>, com registro datado de <strong>${escapeHtml(empresa.abertura)}</strong> junto à Receita Federal do Brasil.</p>
                <p>A empresa encontra-se com situação cadastral <strong>${escapeHtml(empresa.situacao)}</strong>${ds !== ni ? ` desde ${escapeHtml(ds)}` : ""}, mantendo conformidade com os requisitos legais e regulatórios necessários para operação regular no mercado brasileiro.</p>
                <p>Sua atividade econômica principal é classificada pelo CNAE como <strong>${escapeHtml(empresa.cnaePrincipal)}</strong>, refletindo sua atuação no respectivo segmento. O capital social registrado é de <strong>${escapeHtml(empresa.capitalSocial)}</strong>, ${empresa.mei === "Sim" ? "sendo a empresa optante pelo regime de <strong>Microempreendedor Individual (MEI)</strong>" : "não sendo optante pelo regime de Microempreendedor Individual"}.</p>
                <p>Com sede estabelecida em <strong>${escapeHtml(empresa.endereco.cidade)} - ${escapeHtml(empresa.endereco.uf)}</strong>, no endereço ${escapeHtml(empresa.endereco.logradouro)}, ${escapeHtml(empresa.endereco.numero)}${empresa.endereco.complemento ? `, ${escapeHtml(empresa.endereco.complemento)}` : ""}, ${escapeHtml(empresa.endereco.bairro)}, CEP ${escapeHtml(empresa.endereco.cep)}, a empresa está posicionada para atender clientes e parceiros de forma acessível e profissional.</p>
              </div>
              <div class="corp-profile-aside">
                <div class="corp-aside-card">
                  <h4>Ficha Resumida</h4>
                  <dl>
                    <dt>Razão Social</dt><dd>${escapeHtml(empresa.razaoSocial)}</dd>
                    <dt>Nome Fantasia</dt><dd>${escapeHtml(empresa.nomeFantasia)}</dd>
                    <dt>CNPJ</dt><dd>${escapeHtml(empresa.cnpj)}</dd>
                    <dt>Tipo</dt><dd>${escapeHtml(mf)}</dd>
                    <dt>Porte</dt><dd>${escapeHtml(empresa.porte)}</dd>
                    <dt>MEI</dt><dd>${escapeHtml(empresa.mei)}</dd>
                    <dt>Capital Social</dt><dd>${escapeHtml(empresa.capitalSocial)}</dd>
                  </dl>
                </div>
              </div>
            </div>
          </div>
        </section>
        <section id="dados">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Registro Federal Completo</h2><p class="section-text">Dados oficiais extraídos do Cadastro Nacional da Pessoa Jurídica (CNPJ) junto à Receita Federal, incluindo informações cadastrais, situação, enquadramento e endereço fiscal.</p></div>
            <div class="corp-reg-grid">
              <div class="corp-reg-card"><span class="corp-reg-label">Razão Social</span><span class="corp-reg-value">${escapeHtml(empresa.razaoSocial)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Nome Fantasia</span><span class="corp-reg-value">${escapeHtml(empresa.nomeFantasia)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">CNPJ</span><span class="corp-reg-value">${escapeHtml(empresa.cnpj)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Tipo de Estabelecimento</span><span class="corp-reg-value">${escapeHtml(mf)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Situação Cadastral</span><span class="corp-reg-value">${escapeHtml(empresa.situacao)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Data da Situação</span><span class="corp-reg-value">${escapeHtml(ds)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Motivo da Situação</span><span class="corp-reg-value">${escapeHtml(ms)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Data de Abertura</span><span class="corp-reg-value">${escapeHtml(empresa.abertura)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Natureza Jurídica</span><span class="corp-reg-value">${escapeHtml(empresa.naturezaJuridica)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Porte</span><span class="corp-reg-value">${escapeHtml(empresa.porte)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Capital Social</span><span class="corp-reg-value">${escapeHtml(empresa.capitalSocial)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Optante MEI</span><span class="corp-reg-value">${escapeHtml(empresa.mei)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Ente Federativo Resp.</span><span class="corp-reg-value">${escapeHtml(ef)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Situação Especial</span><span class="corp-reg-value">${escapeHtml(se)}</span></div>
              <div class="corp-reg-card"><span class="corp-reg-label">Data Situação Especial</span><span class="corp-reg-value">${escapeHtml(dse)}</span></div>
            </div>
          </div>
        </section>
        <section id="atividades">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Atividade Econômica</h2><p class="section-text">Classificação Nacional de Atividades Econômicas (CNAE) vinculada ao registro da empresa, representando o segmento de atuação junto aos órgãos competentes.</p></div>
            <div class="corp-activity">
              <h3>Atividade Principal</h3>
              <div class="corp-activity-main">${escapeHtml(empresa.cnaePrincipal)}</div>
              <p class="corp-desc">A atividade principal registrada define o segmento econômico preponderante da empresa e é utilizada como referência para obrigações tributárias, enquadramento fiscal e regulação setorial aplicável.</p>
              <h3 style="margin-top: 24px;">Atividades Secundárias</h3>
              ${corpAtividadesHtml}
              <p class="corp-desc" style="margin-top: 12px;">As atividades secundárias representam segmentos complementares em que a empresa pode atuar, ampliando o escopo de serviços e operações permitidos pelo registro.</p>
            </div>
          </div>
        </section>
        <section id="localizacao">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Localização</h2><p class="section-text">Endereço fiscal registrado junto à Receita Federal do Brasil para correspondência e identificação da sede.</p></div>
            <div class="grid-2">
              <div class="card card-large">
                <h3>Endereço Completo</h3>
                <div class="info-list">
                  <div class="info-item"><strong>Logradouro</strong><span>${escapeHtml(empresa.endereco.logradouro)}</span></div>
                  <div class="info-item"><strong>Número</strong><span>${escapeHtml(empresa.endereco.numero)}</span></div>
                  <div class="info-item"><strong>Complemento</strong><span>${escapeHtml(empresa.endereco.complemento || "—")}</span></div>
                  <div class="info-item"><strong>Bairro</strong><span>${escapeHtml(empresa.endereco.bairro)}</span></div>
                  <div class="info-item"><strong>Cidade / UF</strong><span>${escapeHtml(empresa.endereco.cidade)} / ${escapeHtml(empresa.endereco.uf)}</span></div>
                  <div class="info-item"><strong>CEP</strong><span>${escapeHtml(empresa.endereco.cep)}</span></div>
                </div>
                <div class="hero-actions" style="margin-top: 18px;">
                  <a class="btn btn-primary" href="${mapsUrl}" target="_blank" rel="noopener noreferrer">Ver no Google Maps</a>
                </div>
              </div>
            </div>
          </div>
        </section>
        <section id="contato">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Contato</h2><p class="section-text">Canais oficiais de comunicação disponibilizados pela empresa para atendimento, suporte e informações comerciais.</p></div>
            <div class="grid-3">
              <div class="card">
                <h3>Telefone</h3>
                <p>${toTelHref(empresa.contato.telefone) ? `<a href="${escapeHtml(toTelHref(empresa.contato.telefone))}">${escapeHtml(formatTelefoneBr(empresa.contato.telefone))}</a>` : `${escapeHtml(formatTelefoneBr(empresa.contato.telefone))}`}</p>
                <p style="color: var(--muted); font-size: 13px; margin-top: 8px;">Horário comercial, de segunda a sexta-feira.</p>
              </div>
              <div class="card">
                <h3>E-mail</h3>
                <p><a href="mailto:${escapeHtml(empresa.contato.email)}">${escapeHtml(empresa.contato.email)}</a></p>
                <p style="color: var(--muted); font-size: 13px; margin-top: 8px;">Para dúvidas, solicitações ou informações gerais.</p>
              </div>
              <div class="card">
                <h3>Endereço</h3>
                <p>${escapeHtml(enderecoCompleto)}</p>
                <div class="hero-actions" style="margin-top: 14px;"><a class="btn btn-primary" href="${mapsUrl}" target="_blank" rel="noopener noreferrer">Abrir no Maps</a></div>
              </div>
            </div>
          </div>
        </section>
      </main>
      ${footer()}
    `;
    }

    const homeBody = `
      ${header()}
      <main>
        <section class="hero">
          <div class="container">
            <div class="hero-grid">
              <div class="glass-card hero-main">
                <div class="badge">Empresa ativa • MEI • ${escapeHtml(empresa.endereco.cidade)} / ${escapeHtml(empresa.endereco.uf)}</div>
                <h1>${escapeHtml(empresa.razaoSocial)}</h1>
                <p>${_tmplId !== "classic"
                  ? `Empresa regularmente constituída e registrada junto à Receita Federal do Brasil, dedicada a oferecer soluções e serviços com transparência, responsabilidade e compromisso com a excelência no atendimento aos nossos clientes e parceiros comerciais.`
                  : `Página institucional desenvolvida para apresentar informações cadastrais, meios de contato e estrutura básica da empresa com visual dark moderno, navegação elegante e layout profissional.`}</p>
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
            <div class="section-head"><h2 class="section-title">Quem Somos</h2><p class="section-text">${_tmplId !== "classic"
              ? `Somos uma empresa comprometida com a qualidade e a integridade em todas as nossas operações. Atuamos de forma ética e transparente, buscando sempre a satisfação dos nossos clientes e o desenvolvimento sustentável dos nossos negócios.`
              : `Informações gerais e cadastrais exibidas de forma clara, com foco institucional e navegação simples.`}</p></div>
            <div class="grid-3">
              <div class="card"><h3>Natureza Jurídica</h3><p>${escapeHtml(empresa.naturezaJuridica)}</p></div>
              <div class="card"><h3>Porte</h3><p>${escapeHtml(empresa.porte)}</p></div>
              <div class="card"><h3>MEI</h3><p>${escapeHtml(empresa.mei)}</p></div>
            </div>
          </div>
        </section>
        <section id="dados">
          <div class="container">
            <div class="section-head"><h2 class="section-title">Dados Cadastrais</h2><p class="section-text">${_tmplId !== "classic"
              ? `Informações oficiais registradas junto aos órgãos competentes, disponibilizadas para consulta pública e verificação de conformidade empresarial.`
              : `Informações institucionais para referência, contato e validação cadastral.`}</p></div>
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
            <div class="section-head"><h2 class="section-title">Contato</h2><p class="section-text">${_tmplId !== "classic"
              ? `Entre em contato conosco através dos canais oficiais abaixo. Nossa equipe está à disposição para atendê-lo.`
              : `Utilize os canais abaixo para contato direto.`}</p></div>
            <div class="grid-3">
              <div class="card"><h3>Telefone</h3><p>${toTelHref(empresa.contato.telefone) ? `<a href="${escapeHtml(toTelHref(empresa.contato.telefone))}">${escapeHtml(formatTelefoneBr(empresa.contato.telefone))}</a>` : `${escapeHtml(formatTelefoneBr(empresa.contato.telefone))}`}</p></div>
              <div class="card"><h3>E-mail</h3><p><a href="mailto:${escapeHtml(empresa.contato.email)}">${escapeHtml(empresa.contato.email)}</a></p></div>
              <div class="card"><h3>Endereço</h3><p>${escapeHtml(enderecoCompleto)}</p><div class="hero-actions" style="margin-top: 14px;"><a class="btn btn-primary" href="${mapsUrl}" target="_blank" rel="noopener noreferrer">Abrir no Google Maps</a></div></div>
            </div>
          </div>
        </section>
      </main>
      ${footer()}
    `;

    if (path === "/") {
      const _body = _tmplId === "corporate" ? corporateHome() : homeBody;
      return renderPage({ title: `${empresa.razaoSocial} | CNPJ ${empresa.cnpj}`, description: `Página institucional da empresa ${empresa.razaoSocial} (CNPJ ${empresa.cnpj}).`, body: _body });
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
# EMBEDDED_EMPRESA vem como `null` no template; substituímos por um OBJETO JS
# (JSON é JS válido) com os dados do CNPJ já consultados no backend.
_EMPRESA_CONST_RE = re.compile(
    r'(const\s+EMBEDDED_EMPRESA\s*=\s*)null(\s*;)'
)
_TEMPLATE_CONST_RE = re.compile(
    r'(const\s+EMBEDDED_TEMPLATE\s*=\s*)"[^"]*"(\s*;)'
)


def empresa_json_from_cnpjdata(data: Any) -> Optional[str]:
    """Monta o objeto ``empresa`` (formato do template) a partir de um CNPJData
    já consultado — para EMBUTIR no site e não depender da consulta LIVE.

    Devolve uma string JSON (objeto JS válido) ou ``None`` se faltar o mínimo
    (razão social + CNPJ). Best-effort: campos ausentes viram 'Não informado'."""
    if data is None:
        return None

    def _g(attr: str) -> str:
        try:
            v = getattr(data, attr, None)
        except Exception:  # noqa: BLE001
            v = None
        return (str(v).strip() if v is not None else "")

    digits = re.sub(r"\D+", "", _g("cnpj"))
    razao = _g("razao_social")
    if len(digits) != 14 or not razao:
        return None
    cnpj_fmt = (
        f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    )
    ni = "Não informado"
    sec = getattr(data, "atividades_secundarias", None)
    if isinstance(sec, (list, tuple)):
        atividades = [str(s).strip() for s in sec if str(s).strip()]
    else:
        atividades = []
    empresa = {
        "cnpj": cnpj_fmt,
        "razaoSocial": razao or ni,
        "nomeFantasia": _g("nome_fantasia") or ni,
        "abertura": _g("abertura") or ni,
        "situacao": _g("situacao") or "Ativa",
        "naturezaJuridica": _g("natureza_juridica") or ni,
        "porte": _g("porte") or ni,
        "mei": _g("mei") or ni,
        "capitalSocial": _g("capital_social") or ni,
        "matrizFilial": _g("matriz_filial") or "Matriz",
        "dataSituacao": _g("data_situacao") or ni,
        "motivoSituacao": _g("motivo_situacao") or ni,
        "efr": _g("efr") or ni,
        "situacaoEspecial": _g("situacao_especial") or ni,
        "dataSituacaoEspecial": _g("data_situacao_especial") or ni,
        "cnaePrincipal": _g("cnae_principal") or ni,
        "atividadesSecundarias": atividades,
        "endereco": {
            "logradouro": _g("logradouro") or ni,
            "numero": _g("numero") or "S/N",
            "complemento": _g("complemento"),
            "bairro": _g("bairro") or ni,
            "cidade": _g("cidade") or ni,
            "uf": _g("uf") or ni,
            "cep": _g("cep") or ni,
        },
        "contato": {
            "telefone": _g("telefone") or ni,
            "email": _g("email") or ni,
        },
    }
    try:
        return json.dumps(empresa, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return None

# Nome do arquivo da edge function dentro do deploy (netlify/edge-functions/<name>.js)
EDGE_FUNCTION_NAME = "site"


def render_worker_js(
    *, cnpj_digits: str, token_hash: str, empresa_json: Optional[str] = None,
    template_id: Optional[str] = None,
) -> str:
    """Aplica as DUAS substituições no template e devolve o JS do worker.

    Valida invariantes:
      * ``cnpj_digits`` deve ser 14 dígitos numéricos (só dígitos).
      * ``token_hash`` deve ser alfanumérico (Meta usa A-Za-z0-9_-, >=8 chars).
    """
    if not re.fullmatch(r"\d{14}", cnpj_digits or ""):
        raise SiteTemplateError(
            f"cnpj_digits inválido (esperado 14 dígitos): {cnpj_digits!r}"
        )
    # token VAZIO é legítimo na aba "Gerar Site": o site sobe primeiro (sem
    # metatag) e a tag entra no re-deploy da etapa 2 — o template já trata
    # EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION="" (não emite a meta). Quando vem
    # preenchido, continua tendo que ser um hash válido do Meta.
    if token_hash and not re.fullmatch(r"[A-Za-z0-9_-]{8,}", token_hash):
        raise SiteTemplateError(
            f"token_hash inválido (alfanumérico, >=8 chars): {token_hash!r}"
        )
    src = _WORKER_TEMPLATE
    src, n1 = _CNPJ_CONST_RE.subn(
        lambda m: f'{m.group(1)}"{cnpj_digits}"{m.group(2)}', src
    )
    if n1 != 1:
        raise SiteTemplateError(
            f"EMBEDDED_DEFAULT_CNPJ: esperava 1 ocorrência, encontrou {n1}."
        )
    src, n2 = _FB_CONST_RE.subn(
        lambda m: f'{m.group(1)}"{token_hash}"{m.group(2)}', src
    )
    if n2 != 1:
        raise SiteTemplateError(
            f"EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION: esperava 1, encontrou {n2}."
        )
    # EMBEDDED_EMPRESA: se o backend passou os dados já consultados, embute o
    # objeto (JSON = JS válido) no lugar de `null` — o render usa direto e não
    # depende da consulta LIVE (que às vezes falha e abortava o deploy). Sem
    # dados, fica `null` e o comportamento antigo (consulta live) é preservado.
    if empresa_json:
        src, n3 = _EMPRESA_CONST_RE.subn(
            lambda m: f"{m.group(1)}{empresa_json}{m.group(2)}", src, count=1
        )
        if n3 != 1:
            raise SiteTemplateError(
                f"EMBEDDED_EMPRESA: esperava 1 ocorrência, encontrou {n3}."
            )
    if template_id and template_id != "classic":
        src, _nt = _TEMPLATE_CONST_RE.subn(
            lambda m: f'{m.group(1)}"{template_id}"{m.group(2)}', src, count=1
        )
    return src


def to_edge_function(worker_js: str) -> str:
    """Transforma o JS do worker (Cloudflare) em uma Netlify Edge Function.

    Mudanças (cirúrgicas — preservam 100% da lógica de página):
      1. Wrapper: ``export default { async fetch(request, env, ctx) {`` →
         ``export default async (request, context) => {``.
      2. ``ctx`` → ``context`` (apenas nos checks/uso de ``waitUntil``).
      3. ``env`` (leitura de FACEBOOK_DOMAIN_VERIFICATION / DEFAULT_CNPJ) →
         ``Netlify.env.get(...)``.
      4. Cache API da Cloudflare (``caches.default``) — inexistente no edge da
         Netlify (Deno): ``.match`` vira ``null`` e ``.put`` vira no-op.
      5. Fecha a arrow function e declara ``export const config = {path:"/*"}``.
    """
    src = worker_js

    # (1) wrapper de abertura
    open_old = 'export default {\n  async fetch(request, env, ctx) {'
    open_new = 'export default async (request, context) => {'
    if open_old not in src:
        raise SiteTemplateError(
            "wrapper de abertura do worker não encontrado para adaptar ao edge."
        )
    src = src.replace(open_old, open_new, 1)

    # (2) ctx → context (waitUntil)
    src = src.replace(
        'ctx && typeof ctx.waitUntil === "function"',
        'context && typeof context.waitUntil === "function"',
    )
    src = src.replace('ctx.waitUntil(', 'context.waitUntil(')

    # (3) env → Netlify.env
    src = src.replace(
        '(env && (env.FACEBOOK_DOMAIN_VERIFICATION || env.FACEBOOK_VERIFICATION))',
        '((typeof Netlify !== "undefined" && Netlify.env) ? '
        '(Netlify.env.get("FACEBOOK_DOMAIN_VERIFICATION") || '
        'Netlify.env.get("FACEBOOK_VERIFICATION")) : "")',
    )
    src = src.replace(
        '(env && env.DEFAULT_CNPJ)',
        '((typeof Netlify !== "undefined" && Netlify.env) ? '
        'Netlify.env.get("DEFAULT_CNPJ") : "")',
    )

    # (4) Cache API da Cloudflare → no-op (Deno/Netlify edge não tem caches.default)
    src = src.replace('await caches.default.match(cacheKey)', 'null')
    src = src.replace('caches.default.put(cacheKey, responseToCache)', 'Promise.resolve()')

    # (5) fecha a arrow function (última ocorrência de "  },\n};") e declara config
    close_old = '  },\n};'
    idx = src.rfind(close_old)
    if idx == -1:
        raise SiteTemplateError(
            "fechamento do worker não encontrado para adaptar ao edge."
        )
    src = src[:idx] + '};' + src[idx + len(close_old):]
    src = src.rstrip() + '\n\nexport const config = { path: "/*" };\n'
    return src


def render_edge_function(
    *, cnpj_digits: str, token_hash: str, empresa_json: Optional[str] = None,
    template_id: Optional[str] = None,
) -> str:
    """Muta o template com (cnpj, token[, empresa]) e devolve a Edge Function."""
    return to_edge_function(render_worker_js(
        cnpj_digits=cnpj_digits, token_hash=token_hash, empresa_json=empresa_json,
        template_id=template_id,
    ))


def render_multi_edge_function(entries: Any) -> str:
    """Edge Function MULTI-CNPJ (Host-aware): UMA função serve TODOS os
    subdomínios do domínio. Para cada request, lê o subdomínio do Host e busca
    ``{cnpj, token}`` num mapa embutido — assim 1 site (com cert wildcard) cobre
    todos os CNPJs e cada CNPJ novo é só um redeploy do mapa (sem novo cert).

    ``entries``: dict ``{slug: {"cnpj": "...", "token": "..."}}`` ou lista de
    ``{"slug","cnpj","token"}``. ``slug`` é o rótulo do subdomínio (à esquerda).
    """
    # Normaliza para dict slug -> {cnpj, token}, validando os valores.
    m: dict[str, dict[str, str]] = {}
    items = entries.items() if isinstance(entries, dict) else (
        (e.get("slug"), e) for e in entries
    )
    for slug, e in items:
        slug = (slug or "").strip().lower()
        cnpj = "".join(c for c in str(e.get("cnpj") or "") if c.isdigit())
        token = str(e.get("token") or "").strip()
        if not slug:
            continue
        if cnpj and not re.fullmatch(r"\d{14}", cnpj):
            raise SiteTemplateError(f"cnpj inválido p/ slug {slug!r}: {cnpj!r}")
        if token and not re.fullmatch(r"[A-Za-z0-9_-]{8,}", token):
            raise SiteTemplateError(f"token inválido p/ slug {slug!r}: {token!r}")
        m[slug] = {"cnpj": cnpj, "token": token}

    base = to_edge_function(_WORKER_TEMPLATE)
    map_js = json.dumps(m, ensure_ascii=False)

    # Injeta o mapa + resolução por Host logo após a criação do URL.
    anchor = "const url = new URL(request.url);"
    if anchor not in base:
        raise SiteTemplateError("âncora do URL não encontrada para injetar o mapa.")
    inject = (
        anchor
        + f"\n    const EMBEDDED_SITE_MAP = {map_js};"
        + '\n    const __host = (request.headers.get("host") || "").toLowerCase().split(":")[0];'
        + '\n    const __sub = __host.split(".")[0];'
        + "\n    const __entry = EMBEDDED_SITE_MAP[__sub] || {};"
    )
    base = base.replace(anchor, inject, 1)

    # Os 2 literais embutidos passam a vir do mapa (por subdomínio).
    n1 = base.count('const EMBEDDED_DEFAULT_CNPJ = "65144276000199";')
    n2 = base.count('const EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION = "";')
    if n1 != 1 or n2 != 1:
        raise SiteTemplateError(
            f"consts embutidos não encontrados (cnpj={n1}, fb={n2})."
        )
    base = base.replace(
        'const EMBEDDED_DEFAULT_CNPJ = "65144276000199";',
        'const EMBEDDED_DEFAULT_CNPJ = __entry.cnpj || "";', 1,
    )
    base = base.replace(
        'const EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION = "";',
        'const EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION = __entry.token || "";', 1,
    )
    # EMBEDDED_EMPRESA por slug: quando o site foi gerado com dados EDITÁVEIS
    # (aba Gerar Site), o worker serve ESSES dados em vez do lookup LIVE. Sem
    # empresa (fluxo das RUNs) fica null → lookup live (igual antes).
    if 'const EMBEDDED_EMPRESA = null;' in base:
        base = base.replace(
            'const EMBEDDED_EMPRESA = null;',
            'const EMBEDDED_EMPRESA = (__entry && __entry.empresa) || null;', 1,
        )
    if 'const EMBEDDED_TEMPLATE = "classic";' in base:
        base = base.replace(
            'const EMBEDDED_TEMPLATE = "classic";',
            'const EMBEDDED_TEMPLATE = (__entry && __entry.template) || "classic";', 1,
        )
    return base


def render_multi_worker_js(entries: Any) -> str:
    """Worker Cloudflare MULTI-CNPJ (Host-aware): UM worker serve TODOS os
    subdomínios de um domínio. Igual a ``render_multi_edge_function``, mas mantém
    o formato Worker da Cloudflare (sem adaptar para Edge Function da Netlify).

    ``entries``: dict ``{slug: {"cnpj","token"}}`` ou lista de
    ``{"slug","cnpj","token"}``. Cada request lê o subdomínio do Host e busca
    ``{cnpj, token}`` no mapa embutido — 1 Worker (com cert wildcard grátis da
    Cloudflare) cobre todos os CNPJs; CNPJ novo = redeploy do mapa.
    """
    m: dict[str, dict[str, str]] = {}
    items = entries.items() if isinstance(entries, dict) else (
        (e.get("slug"), e) for e in entries
    )
    for slug, e in items:
        slug = (slug or "").strip().lower()
        cnpj = "".join(c for c in str(e.get("cnpj") or "") if c.isdigit())
        token = str(e.get("token") or "").strip()
        if not slug:
            continue
        if cnpj and not re.fullmatch(r"\d{14}", cnpj):
            raise SiteTemplateError(f"cnpj inválido p/ slug {slug!r}: {cnpj!r}")
        if token and not re.fullmatch(r"[A-Za-z0-9_-]{8,}", token):
            raise SiteTemplateError(f"token inválido p/ slug {slug!r}: {token!r}")
        # empresa (opcional): dados EDITÁVEIS já resolvidos (aba Gerar Site).
        # Pode vir como dict OU string JSON. Ausente/ inválido → None (o site
        # cai no lookup LIVE, comportamento das RUNs). Só embute se for dict.
        empresa = e.get("empresa")
        if isinstance(empresa, str) and empresa.strip():
            try:
                empresa = json.loads(empresa)
            except Exception:  # noqa: BLE001
                empresa = None
        if not isinstance(empresa, dict):
            empresa = None
        template = str(e.get("template") or "classic").strip()
        m[slug] = {"cnpj": cnpj, "token": token, "empresa": empresa, "template": template}

    base = _WORKER_TEMPLATE
    map_js = json.dumps(m, ensure_ascii=False)
    anchor = "const url = new URL(request.url);"
    if anchor not in base:
        raise SiteTemplateError("âncora do URL não encontrada para injetar o mapa.")
    inject = (
        anchor
        + f"\n    const EMBEDDED_SITE_MAP = {map_js};"
        + '\n    const __host = (request.headers.get("host") || "").toLowerCase().split(":")[0];'
        + '\n    const __sub = __host.split(".")[0];'
        + "\n    const __entry = EMBEDDED_SITE_MAP[__sub] || {};"
    )
    base = base.replace(anchor, inject, 1)

    n1 = base.count('const EMBEDDED_DEFAULT_CNPJ = "65144276000199";')
    n2 = base.count('const EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION = "";')
    if n1 != 1 or n2 != 1:
        raise SiteTemplateError(
            f"consts embutidos não encontrados (cnpj={n1}, fb={n2})."
        )
    base = base.replace(
        'const EMBEDDED_DEFAULT_CNPJ = "65144276000199";',
        'const EMBEDDED_DEFAULT_CNPJ = __entry.cnpj || "";', 1,
    )
    base = base.replace(
        'const EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION = "";',
        'const EMBEDDED_FACEBOOK_DOMAIN_VERIFICATION = __entry.token || "";', 1,
    )
    # EMBEDDED_EMPRESA por slug: quando o site foi gerado com dados EDITÁVEIS
    # (aba Gerar Site), o worker serve ESSES dados em vez do lookup LIVE. Sem
    # empresa (fluxo das RUNs) fica null → lookup live (igual antes).
    if 'const EMBEDDED_EMPRESA = null;' in base:
        base = base.replace(
            'const EMBEDDED_EMPRESA = null;',
            'const EMBEDDED_EMPRESA = (__entry && __entry.empresa) || null;', 1,
        )
    if 'const EMBEDDED_TEMPLATE = "classic";' in base:
        base = base.replace(
            'const EMBEDDED_TEMPLATE = "classic";',
            'const EMBEDDED_TEMPLATE = (__entry && __entry.template) || "classic";', 1,
        )
    return base
