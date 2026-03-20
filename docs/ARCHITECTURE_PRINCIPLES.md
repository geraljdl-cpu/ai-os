# AI-OS — Architecture Principles

## Regra-mãe

```
Mauro / Ryan / Oneway = linha estável de produção
```

Não se mexe no fluxo atual, exceto: correções críticas, bugs, segurança, disponibilidade.

---

## Linha A — Produção real (protegida)

Mantém-se intocável:
- WhatsApp → validação → faturação → cashflow → planeamento

Serve como: caso real · fonte de dados · fonte de falhas · fonte de aprendizagem.

## Linha B — Evolução paralela

Marketplace, fee model, matching, novos clientes, novos workers:
- Nascem em paralelo, sem tocar na Linha A.

---

## Regra de arquitectura

Tudo o que for novo entra como:
- feature flag / novo módulo / nova tabela / novo endpoint / novo fluxo

Nunca:
- reescrever o que já está a funcionar no caso Mauro/Ryan

---

## Regra de execução (default)

Ao abrir qualquer bloco novo:

```
default = não tocar no fluxo Mauro/Ryan
```

Só tocamos se explicitamente dito:

```
isto entra já em produção
```

---

## Modelo

```
produção = prova
paralelo = evolução
```
