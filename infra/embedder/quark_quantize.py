"""
Quantize bge-small-en-v1.5 ONNX model with AMD Quark for the RyzenAI NPU.

Produces: models/embeddings/bge-small-en-v1.5-quark/model_quantized.onnx
Run with: python infra/embedder/quark_quantize.py
Needs:    /opt/ryzenai/venv activated + /opt/xilinx/xrt/setup.sh sourced
"""
import numpy as np
import os
from pathlib import Path

from transformers import AutoTokenizer
from onnxruntime.quantization.calibrate import CalibrationDataReader
from quark.onnx import ModelQuantizer
from quark.onnx.quantization.config.config import (
    Config, QConfig, QLayerConfig, QuantizationConfig, Int8Spec,
)
from quark.onnx.quantization.quant_utils import ExtendedQuantFormat
from onnxruntime.quantization.quant_utils import QuantType, QuantFormat

BASE = Path(__file__).resolve().parents[2]
ONNX_DIR = BASE / "models/embeddings/bge-small-en-v1.5-onnx"
OUT_DIR  = BASE / "models/embeddings/bge-small-en-v1.5-quark"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CALIB_SENTENCES = [
    "AMD Ryzen AI processor with integrated NPU for machine learning.",
    "Retrieval-augmented generation combines search with language models.",
    "Qdrant is a vector database optimised for nearest-neighbour search.",
    "The BGE embedding model produces dense sentence representations.",
    "System administration requires careful configuration of kernel modules.",
    "OpenNebula manages virtual machine placement across a cluster.",
    "IOMMU isolation is required for NPU PASID-based DMA on Linux.",
    "Vulkan provides a low-overhead GPU compute API for inference.",
    "Quantization reduces model size by representing weights as integers.",
    "Sentence embeddings capture semantic similarity between texts.",
    "The quick brown fox jumps over the lazy dog.",
    "Machine learning workloads benefit from hardware acceleration.",
    "Vector similarity search enables efficient semantic retrieval.",
    "ONNX provides a portable format for neural network models.",
    "Context window size affects the quality of language model outputs.",
    "Tokenisation converts raw text into numerical token sequences.",
    "Attention mechanisms enable transformers to model long-range dependencies.",
    "File system operations require careful sandboxing for security.",
    "Network latency impacts distributed system performance.",
    "Docker containers simplify deployment of microservices.",
]


class SentenceCalibDataReader(CalibrationDataReader):
    def __init__(self, tokenizer, sentences, max_len=128):
        self.data = []
        for sent in sentences:
            enc = tokenizer(
                [sent], return_tensors="np",
                padding="max_length", max_length=max_len, truncation=True,
            )
            self.data.append({
                "input_ids":      enc["input_ids"].astype(np.int64),
                "attention_mask": enc["attention_mask"].astype(np.int64),
                "token_type_ids": enc["token_type_ids"].astype(np.int64),
            })
        self._iter = iter(self.data)

    def get_next(self):
        return next(self._iter, None)

    def rewind(self):
        self._iter = iter(self.data)


def main():
    print("Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(str(ONNX_DIR))

    print("Building calibration data reader...")
    reader = SentenceCalibDataReader(tokenizer, CALIB_SENTENCES)

    print("Configuring Quark static INT8 quantization (NPU transformer mode)...")
    quant_config = QuantizationConfig(
        quant_format=QuantFormat.QDQ,
        activation_type=QuantType.QInt8,
        weight_type=QuantType.QInt8,
        per_channel=False,
        enable_npu_transformer=True,
        optimize_model=True,
        include_cle=True,
    )

    global_layer_config = QLayerConfig(
        activation=Int8Spec(),
        weight=Int8Spec(),
    )
    qconfig = QConfig(global_config=global_layer_config)
    config = Config(global_quant_config=quant_config)

    print("Running quantizer...")
    quantizer = ModelQuantizer(config)
    quantizer.quantize_model(
        model_input=str(ONNX_DIR / "model.onnx"),
        model_output=str(OUT_DIR / "model_quantized.onnx"),
        calibration_data_reader=reader,
    )
    print(f"Quantized model written to {OUT_DIR / 'model_quantized.onnx'}")


if __name__ == "__main__":
    main()
