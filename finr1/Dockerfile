# Fin-R1 (SUFE-AIFLM-Lab, Qwen2.5-7B fine-tuned on financial reasoning) served by Ollama on a Cloud Run L4 GPU.
# The quantised weights (~4.7 GB) are baked into the image so a cold start only loads them into VRAM.
FROM ollama/ollama:latest
ENV OLLAMA_HOST=0.0.0.0:8080 OLLAMA_MODELS=/models OLLAMA_KEEP_ALIVE=-1 OLLAMA_NUM_PARALLEL=2
ARG MODEL=hf.co/mradermacher/Fin-R1-GGUF:Q4_K_M
RUN ollama serve & sleep 5 && ollama pull ${MODEL} && pkill ollama
ENTRYPOINT ["ollama", "serve"]
