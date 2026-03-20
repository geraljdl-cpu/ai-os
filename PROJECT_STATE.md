# AI-OS — PROJECT STATE (single source of truth)

## Objetivo
Sistema operacional pessoal/empresa que automatiza:
- captura → análise → decisão → execução → faturação

## Princípios
- Fechar ciclos (módulos DONE)
- Evitar dispersão
- Cada módulo gera valor sozinho
- Automação > interface

## Estado atual (2026-03)
DONE v1:
- Comercial (pedidos → orçamento → PDF → email)
- Seguros (alertas + worker + dashboard + ingest PDF básico)
- Quick Capture (web)
- AI Council (análise estruturada, persistência OK)

EM EXECUÇÃO:
- Quick Capture via Telegram

PENDENTE IMEDIATO (ordem fixa):
1. Telegram Quick Capture (validar E2E)
2. PDF Seguros (validar com PDFs reais)
3. Exportação Excel (faturação, horas, seguros)
4. RH / horas / faturação

BACKLOG CONTROLADO (não executar agora):
- Council Chat (multi-agent, GPT/Claude/Gemini)
- OCR avançado PDFs
- Integrações externas (Fidelidade API, etc.)
- Otimizações cluster/infra

## Regras de execução
- Não iniciar novo módulo sem fechar o atual
- Validar sempre E2E (UI → API → DB → UI)
- Entregar sempre:
  - ficheiros alterados
  - migrações SQL
  - endpoints tocados
  - passos de teste

## Convenções
- Origem dos dados (source): web / telegram / email
- Tabelas:
  - ideas, tasks, decisions
  - council_reviews
  - insurance_policies, insurance_documents
- Logs e jobs com timestamps claros

## Critérios de DONE (geral)
- Fluxo completo automático
- Persistência confirmada
- Recarregar página mantém estado
- Teste com dados reais
