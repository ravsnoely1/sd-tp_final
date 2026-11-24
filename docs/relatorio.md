# Sistema de Chat Distribuído — Prosa

**Disciplina:** Sistemas Distribuídos — CEFET-MG  
**Referência:** enunciado SD_TP_Final_2026, professora Michelle Hanne, 2026/1  
**Data dos ensaios desta entrega:** 24/11/2026  
**Autor(es):** preencher antes da entrega  
**URL do código-fonte:** pendente de publicação pelo autor

## 1. Objetivo e escopo

O Prosa permite trocar mensagens privadas e em grupo por uma interface web. A solução separa usuários e autenticação da lógica de mensagens, mantém o histórico em banco de dados e distribui conexões entre réplicas do serviço de chat. O projeto foi desenvolvido em Python, com aiohttp no backend e HTML, CSS e JavaScript no frontend.

O escopo inclui cadastro, login, logout, criação de conversas, seleção dos participantes, envio em tempo real e consulta paginada do histórico. Os grupos guardam nome, participantes, responsável e data de criação. Cada mensagem registra autor, conteúdo, conversa e data/hora. Alteração de participantes, anexos e confirmação de leitura não fazem parte desta versão.

## 2. Decisões de projeto

O serviço de autenticação oferece endpoints HTTP para cadastro, login, validação de sessão, logout e listagem de usuários. As senhas são protegidas com scrypt e salt individual. Os tokens são aleatórios, têm validade de oito horas e são armazenados no banco somente como hash. O logout remove a sessão. No navegador, o token fica em sessionStorage.

O serviço de mensagens consulta a autenticação por HTTP antes das operações protegidas. As conexões WebSocket recebem a credencial no primeiro frame, evitando colocá-la na URL. O servidor revalida a sessão a cada envio e durante períodos ociosos. Além da autenticação, verifica se o usuário participa da conversa antes de permitir leitura ou envio.

A implantação proposta usa PostgreSQL para dados de usuários, sessões, conversas, participantes, mensagens e eventos. Os serviços compartilham a instância do banco, mas o código de autenticação administra suas próprias tabelas. Essa escolha simplifica a implantação acadêmica; não representa isolamento completo de armazenamento entre microsserviços.

Para execução sem Docker, existe um modo de laboratório com SQLite e processos independentes. Ele permite testar as interações no mesmo computador. O arquivo SQLite compartilhado não deve ser interpretado como solução para armazenamento entre máquinas distintas.

## 3. Comunicação e funcionamento distribuído

Na configuração Docker Compose, o Nginx encaminha requisições de autenticação a duas réplicas e conexões de chat a outras duas réplicas. Novos endereços são resolvidos pelo DNS do Docker. Cada conexão WebSocket permanece no servidor que a aceitou até ser encerrada. A inclusão de novas réplicas amplia a quantidade de processos que podem manter conexões, sem exigir afinidade de sessão.

O principal problema dessa distribuição é entregar uma mensagem quando remetente e destinatário estão em servidores diferentes. Para isso, a gravação da mensagem também insere um evento na mesma transação. Cada réplica consulta o log de eventos, identifica os participantes e transmite o evento aos seus próprios clientes. O intervalo normal de consulta é de aproximadamente 80 ms, sujeito ao tempo de banco e à fila de eventos.

As escritas usam um lock transacional global para ordenar os commits do log: advisory lock no PostgreSQL e transação de escrita no SQLite. Assim, o cursor de uma réplica não avança sobre um identificador cuja transação ainda não foi confirmada. Essa escolha facilita a correção, mas serializa escritas e limita o crescimento da vazão.

O cliente associa cada envio a um identificador. Repetições com o mesmo autor, identificador, conversa e conteúdo devolvem a mensagem já gravada. Isso evita duplicação quando uma confirmação se perde. Após desconectar, a interface tenta reconectar, recarrega as conversas e o histórico recente e reenvia os envios pendentes mantidos em memória. Fechar a aba descarta essas pendências. A confirmação `ack` significa que a mensagem foi persistida, sem garantir que o destinatário a leu.

## 4. Método de avaliação

Os testes unitários avaliam credenciais, revogação e expiração de sessões, validação de entradas, criação de grupos, autorização, persistência, paginação e repetição concorrente de um envio. Os testes de integração iniciam servidores HTTP reais em portas locais, com autenticação e duas réplicas de chat, e verificam o fluxo login–conversa–mensagem–histórico.

Também há um cenário de falha: o teste desliga uma réplica, abre uma nova conexão pelo balanceador e confirma o envio e a consulta do histórico pela réplica sobrevivente. Esse ensaio cobre a falha de um processo de chat no laboratório. Não substitui a avaliação do Nginx, do Docker ou da recuperação automática da interface no navegador.

O script de carga cadastra usuários exclusivos e dispara os logins em tarefas concorrentes. Em seguida, mantém 100 conexões WebSocket autenticadas ao mesmo tempo, cria 20 grupos de cinco participantes e realiza três rodadas de envio. Cada usuário envia uma mensagem ao seu grupo em cada rodada, totalizando 300 mensagens. Uma mensagem privada adicional verifica o fluxo 1:1 durante a carga.

A verificação compara os identificadores de todas as mensagens e destinatários esperados, confere as confirmações de persistência e percorre o histórico. As métricas de entrega medem o intervalo entre o envio no cliente de teste e o recebimento do evento em cada destinatário, usando o relógio monotônico do mesmo processo. Incluem fila, processamento e transporte local; não são medições de rede entre máquinas.

## 5. Resultados observados

O ambiente executado foi Windows, Python 3.13, aiohttp 3.13.5, SQLite em arquivo local, um serviço de autenticação, dois processos de chat e um proxy Python round-robin. Os dados abaixo pertencem à execução `8f8ead03`, registrada em `results/load-local.json`.

| Indicador | Resultado |
| --- | ---: |
| Usuários/WebSockets simultâneos | 100 |
| Grupos | 20 |
| Conexões na réplica chat-1 | 50 |
| Conexões na réplica chat-2 | 50 |
| Mensagens enviadas | 301 |
| Confirmações de persistência | 301 |
| Entregas esperadas | 1.502 |
| Entregas observadas | 1.502 |
| Histórico | Conferido por identificador |
| Mediana da latência de entrega | 1.489,45 ms |
| Percentil 95 da latência de entrega | 2.380,39 ms |
| Duração dos logins | 3,673 s |
| Duração da fase de mensagens | 2,853 s |
| Duração total | 12,446 s |
| Erros no ensaio concluído | 0 |

As 1.502 entregas correspondem a 300 mensagens de grupo recebidas por cinco participantes, incluindo o remetente, mais uma mensagem privada recebida pelas duas pessoas. Não houve perda detectada nesse conjunto. As confirmações são contabilizadas separadamente das entregas.

A distribuição 50/50 demonstra o funcionamento do balanceamento local, e a entrega entre réplicas foi observada. Entretanto, a mediana próxima de 1,5 s e o p95 próximo de 2,4 s indicam atraso sob a rajada utilizada. Portanto, o ensaio não sustenta uma afirmação geral de baixa latência ou eficiência em produção.

Durante a preparação do ensaio, o limite padrão de conexões do cliente HTTP do proxy local impediu chamadas HTTP após a abertura de 100 WebSockets. A configuração foi corrigida para que essas conexões persistentes não esgotassem o pool. Os resultados da tabela são da execução completa posterior à correção.

Os 14 testes unitários e de integração passaram. O registro textual está em `results/unit-integration.txt`. A sintaxe JavaScript foi verificada com `node --check`; isso não comprova a interação no navegador. A interface responsiva ainda precisa de inspeção visual e de testes manuais completos.

## 6. Limitações e avaliação pendente

Não havia Docker instalado no ambiente de desenvolvimento. Por isso, não foram executados o PostgreSQL, as imagens dos serviços, o Nginx ou a descoberta dinâmica de containers. A configuração Compose teve sua sintaxe YAML verificada, mas a implantação completa permanece pendente de validação.

Uma carga única com duas réplicas comprova que houve distribuição e comunicação entre elas nas condições avaliadas. Para avaliar ganho de escalabilidade horizontal, é necessário repetir o mesmo ensaio com uma, duas e três réplicas, mantendo hardware e carga constantes, e comparar latência, vazão e erros. O README fornece os comandos e orientações para essa comparação e para a falha de um container.

O PostgreSQL e o gateway não possuem redundância nessa configuração. Uma falha nesses componentes, ou no único computador hospedeiro, interrompe o serviço. Logo, a tolerância demonstrada se restringe à falha de uma réplica de chat no laboratório. Replicação do banco, failover do gateway e execução em múltiplos hosts são melhorias necessárias para ampliar a disponibilidade.

O log de eventos também não possui política de retenção, e cada operação abre uma conexão com o banco. Para ampliar a capacidade, devem ser avaliados pool de conexões, redução da seção crítica de escrita, retenção segura e mecanismos de distribuição de eventos mais eficientes. Uma exposição pública ainda requer HTTPS e proteção contra abuso e tentativas repetidas de login.

Antes da entrega acadêmica, devem ser preenchidos os autores e a URL do repositório. Esta versão do relatório é Markdown, sem paginação fixa: exporte para PDF e confira o limite de dez páginas estabelecido no enunciado. Nenhum código foi publicado automaticamente.

## Referências

- Enunciado fornecido: *Sistemas Distribuídos — Trabalho Final: Sistema de Chat Distribuído*, CEFET-MG, 2026/1.
- aiohttp. *Server Reference*. https://docs.aiohttp.org/en/stable/web_reference.html
- Psycopg. *Basic module usage*. https://www.psycopg.org/psycopg3/docs/basic/usage.html
- Psycopg. *Row factories*. https://www.psycopg.org/psycopg3/docs/advanced/rows.html
