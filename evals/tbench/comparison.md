# TB2.0 — sliceagent (gpt-5.5 API) vs codex (subscription)

Reward 1=pass 0=fail. Wall = agent-phase seconds. Tokens = in/out. Steps = tool/agent steps.

| task | mem rew | cdx rew | mem wall | cdx wall | mem tok(in/out) | cdx tok(in/out) | mem steps | cdx steps | flag |
|---|---|---|---|---|---|---|---|---|---|
| break-filter-js-from-html | 0 | 0 | 115 | 276 | 77,001/2,659 | 354,304/3,817 | 10 | 28 |  |
| cancel-async-tasks | 0 | 0 | 55 | 111 | 40,757/2,313 | 92,544/968 | 6 | 12 |  |
| crack-7z-hash | 0 | ENV | 900 | 900 | -/- | -/- | - | - |  |
| db-wal-recovery | - | - | - | - | -/- | -/- | - | - |  |
| dna-insert | 0 | ENV | 171 | 2 | 180,167/6,344 | -/- | 15 | - |  |
| extract-elf | ENV | 1 | 123 | 141 | 92,654/4,354 | 96,896/1,484 | 9 | 30 |  |
| fix-git | 1 | - | 56 | - | 59,211/1,505 | -/- | 8 | - |  |
| kv-store-grpc | 1 | 0 | 298 | 236 | 66,301/2,729 | 238,336/554 | 9 | 28 |  |
| log-summary-date-ranges | 1 | 1 | 53 | 129 | 28,405/2,055 | 61,312/176 | 4 | 8 |  |
| mteb-leaderboard | - | - | - | - | -/- | -/- | - | - |  |
| openssl-selfsigned-cert | 1 | 1 | 80 | 143 | 39,021/3,299 | 82,560/40 | 5 | 12 |  |
| overfull-hbox | - | 0 | - | 155 | -/- | 156,800/2,544 | - | 20 |  |
| password-recovery | 1 | - | 86 | - | 209,743/4,095 | -/- | 15 | - |  |
| polyglot-rust-c | 0 | - | 868 | - | 921,936/18,128 | -/- | 60 | - |  |
| prove-plus-comm | 1 | - | 39 | - | 37,102/805 | -/- | 6 | - |  |
| query-optimize | - | - | - | - | -/- | -/- | - | - |  |
| regex-log | 1 | 1 | 103 | 106 | 42,745/5,083 | 77,440/1,331 | 6 | 10 |  |
| reshard-c4-data | ENV | 1 | 1,643 | 496 | 255,834/8,098 | 388,608/2,602 | 12 | 40 |  |
| sanitize-git-repo | 0 | 1 | 352 | 404 | 134,924/10,154 | 384,256/976 | 9 | 36 | **sliceagent INFERIOR** |
| vulnerable-secret | 1 | 0 | 73 | 102 | 124,090/1,704 | -/- | 10 | 2 |  |
| code-from-image | ENV | 1 | 1,200 | 227 | -/- | 144,896/600 | - | 10 |  |
| constraints-scheduling | 1 | 1 | 145 | 84 | 75,593/10,845 | 43,008/555 | 7 | 14 |  |
| count-dataset-tokens | 0 | 1 | 900 | 664 | -/- | 698,752/2,094 | - | 30 | **sliceagent INFERIOR** |
| financial-document-processor | ENV | - | 1,200 | - | -/- | -/- | - | - |  |
| fix-code-vulnerability | 1 | 1 | 125 | 331 | 718,676/4,354 | 350,336/505 | 14 | 34 |  |
| git-leak-recovery | 1 | - | 58 | - | 48,462/3,291 | -/- | 7 | - |  |
| git-multibranch | 1 | 1 | 279 | 343 | 118,185/10,885 | 668,544/2,486 | 10 | 54 |  |
| headless-terminal | 1 | - | 98 | - | 60,754/3,506 | -/- | 8 | - |  |
| mailman | 0 | 1 | 1,800 | 736 | -/- | 2,696,960/2,012 | - | 132 | **sliceagent INFERIOR** |
| modernize-scientific-stack | 0 | 0 | 284 | 201 | 79,971/3,583 | 102,912/133 | 9 | 16 |  |
| nginx-request-logging | 1 | 1 | 140 | 158 | 56,972/3,268 | 195,328/292 | 6 | 16 |  |
| polyglot-c-py | 0 | 0 | 175 | 165 | 83,084/8,438 | 117,376/1,303 | 11 | 20 |  |
| pypi-server | 1 | 0 | 283 | 455 | 66,928/4,285 | 403,200/1,417 | 8 | 39 |  |
| pytorch-model-cli | ENV | 0 | 159 | 514 | 157,180/8,904 | 485,760/1,523 | 14 | 66 |  |
| sparql-university | 1 | 1 | 90 | 96 | 89,589/4,578 | 99,840/982 | 8 | 26 |  |
| sqlite-db-truncate | ENV | 1 | 76 | 158 | 52,126/3,077 | 153,984/582 | 7 | 14 |  |
| write-compressor | 0 | 1 | 353 | 153 | 25,659/2,123 | 81,536/2,176 | 5 | 22 | **sliceagent INFERIOR** |
| largest-eigenval | 1 | 0 | 616 | 1 | 214,609/6,248 | -/- | 21 | - |  |
| qemu-startup | 0 | 0 | 900 | 900 | -/- | -/- | - | - |  |
| build-cython-ext | 0 | - | 900 | - | -/- | -/- | - | - |  |
| build-pmars | 1 | 1 | 230 | 878 | 117,045/2,750 | 1,835,904/1,765 | 10 | 84 |  |
| chess-best-move | 1 | 1 | 857 | 406 | 516,905/17,392 | 328,960/2,747 | 31 | 30 |  |
| circuit-fibsqrt | 1 | 1 | 877 | 546 | 443,225/25,051 | 360,960/3,908 | 26 | 26 |  |
| custom-memory-heap-crash | 0 | 1 | 1,120 | 282 | 90,771/4,380 | 355,328/1,774 | 10 | 50 | **sliceagent INFERIOR** |
| distribution-search | 1 | - | 125 | - | 75,294/3,706 | -/- | 8 | - |  |
| dna-assembly | 0 | 0 | 556 | 488 | 127,704/9,323 | 412,288/4,779 | 12 | 36 |  |
| large-scale-text-editing | 0 | - | 1,800 | - | -/- | -/- | - | - |  |
| model-extraction-relu-logits | 0 | 0 | 356 | 61 | 24,439/1,843 | -/- | 5 | 8 |  |
| path-tracing | 1 | 1 | 815 | 1,800 | 547,975/6,333 | -/- | 21 | - |  |
| path-tracing-reverse | 1 | 1 | 492 | 346 | 2,957,613/14,616 | 1,510,272/4,088 | 42 | 70 |  |
| pytorch-model-recovery | - | - | - | - | -/- | -/- | - | - |  |
| qemu-alpine-ssh | 0 | - | 900 | - | -/- | -/- | - | - |  |
| raman-fitting | 0 | - | 557 | - | 213,558/15,957 | -/- | 20 | - |  |
| rstan-to-pystan | 0 | 0 | 1,800 | 2,700 | -/- | -/- | - | - |  |
| tune-mjcf | 0 | 0 | 214 | 367 | 82,053/5,243 | 267,392/2,775 | 10 | 38 |  |
| video-processing | 0 | 0 | 562 | 591 | 622,406/11,900 | 580,224/3,496 | 22 | 24 |  |

**Completed:** sliceagent 45/56, codex 38/56
**Pass rate:** sliceagent 23/45 · codex 22/38
**Total agent wall:** sliceagent 361m · codex 266m
**Total tokens:** sliceagent 9,663,644 · codex 13,883,300
**sliceagent INFERIOR on 5:** sanitize-git-repo, count-dataset-tokens, mailman, write-compressor, custom-memory-heap-crash
**ENV/verifier-infra failures (need re-run, not a real verdict) on 8:** code-from-image, crack-7z-hash, dna-insert, extract-elf, financial-document-processor, pytorch-model-cli, reshard-c4-data, sqlite-db-truncate