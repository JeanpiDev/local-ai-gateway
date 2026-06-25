"""Modelos Pydantic compatibles con la API de OpenAI.

Se valida lo mínimo necesario (model, messages) y se permite el resto de campos
(`extra="allow"`) para reenviarlos tal cual al backend (temperature, max_tokens, etc.).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: Any = None  # str o lista de partes (multimodal) — se reenvía tal cual


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "model": "qwen2.5:3b-instruct",
                "messages": [
                    {"role": "user", "content": "Resume en una línea qué es un gateway de IA."}
                ],
                "stream": False,
                "max_tokens": 128,
            }
        },
    )

    model: str = Field(description="ID del modelo (ver GET /v1/models).")
    messages: list[ChatMessage] = Field(
        min_length=1, description="Historial de mensajes estilo OpenAI."
    )
    stream: bool = Field(default=False, description="Si es true, responde SSE en streaming.")

    def to_upstream_payload(self) -> dict[str, Any]:
        """Serializa preservando los campos extra para reenviar al backend."""
        return self.model_dump(exclude_none=True)


class ProvisionUserRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Ana Pérez",
                "email": "ana@empresa.com",
                "password": "una-contraseña-fuerte",
                "role": "user",
            }
        }
    )

    name: str = Field(description="Nombre visible del usuario.")
    email: str = Field(description="Email único; será su login en Open WebUI.")
    password: str = Field(description="Contraseña inicial del usuario.")
    role: Literal["user", "admin", "pending"] = "user"


class ProvisionUserResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str
    api_key: str = Field(description="API key personal (sk-...) para consumir el gateway.")
