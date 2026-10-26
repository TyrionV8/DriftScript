# DriftScript ✂️🎬

**DriftScript** é um addon de edição de vídeo baseada em texto (estilo *Descript*) e servidor **MCP (*Model Context Protocol*)** para o editor de vídeo **CutWire Drift**.

Permite cortar vídeos editando diretamente o texto transcrito — tanto manualmente pela interface visual quanto pedindo a modelos de IA via MCP para remover vícios de linguagem, gaguejos, hesitações ou trechos específicos.

---

## Principais Recursos

1. **Edição Estilo Descript com Palavras Riscadas**:
   - **Clique em qualquer palavra**: move imediatamente o player de pré-visualização e a agulha (*playhead*) do **CutWire Drift** para o início exato daquela palavra.
   - **Palavras removidas não somem**: permanecem visíveis e **riscadas** (`line-through`), permitindo audição e restauração a qualquer momento com um clique.
   - **Preview Instantâneo**: pula automaticamente os trechos riscados em tempo real sem necessidade de renderização prévia.
   - **Sincronização Direta com o Drift**: reconstrói os cortes na timeline do Drift preservando escala, posição, efeitos e aplicando rampas anti-clique (`declick`) em **um único passo de Desfazer (`Ctrl+Z`)**.

2. **Três Perfis de Memória Whisper (Com Monitoramento de VRAM ao Vivo)**:
   - **⚡ `fast` (`small`)**: `~1.2 GB VRAM` — Transcrição ultrarrápida com baixo uso de memória.
   - **⚖️ `normal` (`medium`)**: `~2.8 GB VRAM` — Equilíbrio ideal entre precisão em português/inglês e memória.
   - **🧠 `heavy` (`large-v3`)**: `~4.8 GB VRAM` — Máxima precisão lexical para termos técnicos e fala rápida.
   - Painel superior exibe em tempo real o uso de **VRAM da GPU**, a projeção de consumo do modelo selecionado, uso de RAM e botão para **liberar VRAM** da placa de vídeo.

3. **Compatibilidade Total com Processadores Sem AVX2 + Prioridade para GPU**:
   - Projetado para rodar em GPUs NVIDIA via **CUDA (`float16`)**, evitando qualquer instrução vetorial **AVX2** na CPU (compatível com processadores Intel Xeon Ivy Bridge / Sandy Bridge e equivalentes).

4. **Sincronização Acústica de Tempo de Alta Precisão**:
   - **Refinamento por Ilhas de Energia RMS (10 ms)**: elimina silêncios iniciais/finais absorvidos pelo alinhador fonético, garantindo que cada palavra comece e termine no exato milissegundo acústico da fala.
   - **Snap em Vales de Energia**: posiciona os cortes nos pontos de menor energia acústica mantendo margem protetora configurável (`padding`, padrão `35 ms`) ao redor das palavras mantidas.
   - **Conversão Bidirecional `Source Time ⇄ Drift Timeline Time`**: calcula a posição exata no Drift mesmo após dezenas de cortes e *ripple deletes*.

5. **Servidor MCP Integrado para Modelos de IA**:
   - Expõe ferramentas MCP em `http://127.0.0.1:4732/mcp` e via `stdio` (`python -m driftscript.mcp_server`).
   - Agentes de IA podem consultar a transcrição indexada palavra por palavra e cortar vícios de linguagem, repetições ou frases inteiras mantendo a interface web e o Drift sincronizados ao vivo.

---

## Estrutura do Repositório

```text
DriftScript/
├── README.md
├── pyproject.toml
├── requirements.txt
├── run.sh
├── driftscript/
│   ├── __init__.py
│   ├── config.py          # Perfis Fast/Normal/Heavy e diagnóstico de VRAM/RAM/AVX2
│   ├── drift_client.py    # Cliente JSON-RPC para o servidor MCP do CutWire Drift (porta 4731)
│   ├── transcriber.py     # Motor Whisper CUDA fp16 (sem AVX2) + refinamento acústico RMS 10ms
│   ├── sync_engine.py     # Matemática de sincronia Source Time <-> Drift Timeline Time
│   ├── state.py           # Estado compartilhado, histórico Undo/Redo e detector de vícios
│   ├── mcp_server.py      # Servidor MCP (HTTP + stdio) para edição por agentes de IA
│   ├── server.py          # Servidor FastAPI + Streaming HTTP 206 + Web UI
│   └── static/            # Interface Descript-style (index.html, styles.css, app.js)
└── tests/
    └── test_sync.py       # Testes automatizados de sincronização de tempo e cortes
```

---

## Como Executar

```bash
./run.sh
```

Acesse no navegador: **`http://127.0.0.1:4732`**

---

## Ferramentas MCP Disponíveis para Agentes de IA

| Ferramenta MCP | Descrição |
|---|---|
| `driftscript_memory_status` | Consulta VRAM/RAM em tempo real, modelo carregado e consumo dos perfis `fast`, `normal` e `heavy` (ou libera VRAM com `unload: true`). |
| `driftscript_list_clips` | Lista as mídias e clipes abertos na timeline do CutWire Drift. |
| `driftscript_transcribe` | Transcreve (ou carrega do cache) o áudio de um clipe nos modos `fast`, `normal` ou `heavy`. |
| `driftscript_get_transcript` | Lê a transcrição com índices `[i]` por palavra (`[i] ~palavra~` quando riscada) e pausas. |
| `driftscript_search_words` | Busca termos específicos e detecta automaticamente vícios de linguagem, gaguejos/repetições e pausas longas. |
| `driftscript_cut_words` | Risca (`deleted=true`) palavras por `indices` ou `ranges` (`[[inicio, fim]]`) e aplica os cortes na timeline do Drift. |
| `driftscript_restore_words` | Restaura palavras riscadas (`indices`, `ranges` ou `restore_all: true`) e atualiza o Drift. |
| `driftscript_seek_word` | Move o playhead do Drift e do player web exatamente para a palavra `word_index`. |
| `driftscript_sync_drift` | Força a sincronização imediata dos cortes (`keep_ranges`) com a timeline do Drift. |
