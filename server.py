from fastapi import FastAPI, Request, HTTPException, Form
import uvicorn
import logging
import json
import traceback
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional, Union, Literal
import httpx
import os
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import litellm
# Configure litellm to drop unsupported parameters
litellm.drop_params = True
import uuid
import time
from dotenv import load_dotenv
import re
from datetime import datetime
import sys
import webbrowser
import threading

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.ERROR,  # Only show errors
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        # Remove default stdout handler by only defining a NullHandler
        logging.NullHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configure uvicorn to be quieter
import uvicorn
# Tell uvicorn's loggers to be quiet
logging.getLogger("uvicorn").setLevel(logging.ERROR)
logging.getLogger("uvicorn.access").setLevel(logging.ERROR)
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)

# Create a filter to block any log messages containing specific strings
class MessageFilter(logging.Filter):
    def filter(self, record):
        # Block messages containing these strings
        blocked_phrases = [
            "LiteLLM completion()",
            "HTTP Request:",
            "selected model name for cost calculation",
            "utils.py",
            "cost_calculator"
        ]

        if hasattr(record, 'msg') and isinstance(record.msg, str):
            for phrase in blocked_phrases:
                if phrase in record.msg:
                    return False
        return True

# Apply the filter to the root logger to catch all messages
root_logger = logging.getLogger()
root_logger.addFilter(MessageFilter())

# Custom formatter for model mapping logs
class ColorizedFormatter(logging.Formatter):
    """Custom formatter to highlight model mappings"""
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def format(self, record):
        if record.levelno == logging.debug and "MODEL MAPPING" in record.msg:
            # Apply colors and formatting to model mapping logs
            return f"{self.BOLD}{self.GREEN}{record.msg}{self.RESET}"
        return super().format(record)

# Apply custom formatter to console handler
for handler in logger.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.setFormatter(ColorizedFormatter('%(asctime)s - %(levelname)s - %(message)s'))

# Get API keys from environment
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Set default models, user can change them in the UI
BIG_MODEL = "o3-mini"
SMALL_MODEL = "claude-3-haiku-20240307"

# Store request history for UI display
REQUEST_HISTORY = []  # Items will be inserted at the front, so newest requests are always at the top
MAX_HISTORY = 50  # Maximum number of requests to keep in history

# Create directory for templates if it doesn't exist
os.makedirs("templates", exist_ok=True)

# Set up templates directory for the UI
templates = Jinja2Templates(directory="templates")

# Flag to enable model swapping between Anthropic and OpenAI
# Set based on the selected models
if "claude" in BIG_MODEL.lower() and "claude" in SMALL_MODEL.lower():
    USE_OPENAI_MODELS = False
    logger.debug(f"Using Claude models exclusively - disabling OpenAI model swapping")
else:
    USE_OPENAI_MODELS = True
    logger.debug(f"Using non-Claude models - enabling model swapping")

app = FastAPI()

# Models for Anthropic API requests
class ContentBlockText(BaseModel):
    type: Literal["text"]
    text: str

class ContentBlockImage(BaseModel):
    type: Literal["image"]
    source: Dict[str, Any]

class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]

class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]], Dict[str, Any], List[Any], Any]

class SystemContent(BaseModel):
    type: Literal["text"]
    text: str

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: Union[str, List[Union[ContentBlockText, ContentBlockImage, ContentBlockToolUse, ContentBlockToolResult]]]

class Tool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any]

class ThinkingConfig(BaseModel):
    enabled: bool

class MessagesRequest(BaseModel):
    model: str
    max_tokens: int
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    thinking: Optional[ThinkingConfig] = None
    reasoning_effort: Optional[str] = None  # Added for OpenAI o1 and o3-mini models
    original_model: Optional[str] = None  # Will store the original model name

    @field_validator('model')
    def validate_model(cls, v, info):
        # Store the original model name
        original_model = v

        # Check if we're using OpenAI models and need to swap
        if USE_OPENAI_MODELS:
            # Remove anthropic/ prefix if it exists
            if v.startswith('anthropic/'):
                v = v[10:]  # Remove 'anthropic/' prefix

            # Swap Haiku with small model (default: gpt-4o-mini)
            if 'haiku' in v.lower():
                # If small model starts with "claude", keep original model with anthropic/ prefix
                if SMALL_MODEL.startswith("claude"):
                    # Ensure we use the anthropic/ prefix for Claude models
                    if not original_model.startswith("anthropic/"):
                        new_model = f"anthropic/{v}"
                    else:
                        new_model = original_model  # Keep the original model as-is

                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model} (CLAUDE)")
                    v = new_model
                else:
                    # Use OpenAI model
                    new_model = f"openai/{SMALL_MODEL}"
                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")
                    v = new_model

            # Swap any Sonnet model with big model (default: gpt-4o)
            elif 'sonnet' in v.lower():
                # If big model starts with "claude", keep original model with anthropic/ prefix
                if BIG_MODEL.startswith("claude"):
                    # Ensure we use the anthropic/ prefix for Claude models
                    if not original_model.startswith("anthropic/"):
                        new_model = f"anthropic/{v}"
                    else:
                        new_model = original_model  # Keep the original model as-is

                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model} (CLAUDE)")
                    v = new_model
                else:
                    # Use OpenAI model
                    new_model = f"openai/{BIG_MODEL}"
                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")
                    v = new_model

            # Keep the model as is but add openai/ prefix if not already present
            elif not v.startswith('openai/') and not v.startswith('anthropic/'):
                new_model = f"openai/{v}"
                logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")
                v = new_model

            # Store the original model in the values dictionary
            # This will be accessible as request.original_model
            values = info.data
            if isinstance(values, dict):
                values['original_model'] = original_model

            return v
        else:
            # Original behavior - ensure anthropic/ prefix
            original_model = v
            if not v.startswith('anthropic/'):
                new_model = f"anthropic/{v}"
                logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")

                # Store original model
                values = info.data
                if isinstance(values, dict):
                    values['original_model'] = original_model

                return new_model
            return v

class TokenCountRequest(BaseModel):
    model: str
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    tools: Optional[List[Tool]] = None
    thinking: Optional[ThinkingConfig] = None
    tool_choice: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None  # Added for OpenAI o1 and o3-mini models
    original_model: Optional[str] = None  # Will store the original model name

    @field_validator('model')
    def validate_model(cls, v, info):
        # Store the original model name
        original_model = v

        # Same validation as MessagesRequest
        if USE_OPENAI_MODELS:
            # Remove anthropic/ prefix if it exists
            if v.startswith('anthropic/'):
                v = v[10:]

            # Swap Haiku with small model (default: gpt-4o-mini)
            if 'haiku' in v.lower():
                # If small model starts with "claude", keep original model with anthropic/ prefix
                if SMALL_MODEL.startswith("claude"):
                    # Ensure we use the anthropic/ prefix for Claude models
                    if not original_model.startswith("anthropic/"):
                        new_model = f"anthropic/{v}"
                    else:
                        new_model = original_model  # Keep the original model as-is

                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model} (CLAUDE)")
                    v = new_model
                else:
                    # Use OpenAI model
                    new_model = f"openai/{SMALL_MODEL}"
                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")
                    v = new_model

            # Swap any Sonnet model with big model (default: gpt-4o)
            elif 'sonnet' in v.lower():
                # If big model starts with "claude", keep original model with anthropic/ prefix
                if BIG_MODEL.startswith("claude"):
                    # Ensure we use the anthropic/ prefix for Claude models
                    if not original_model.startswith("anthropic/"):
                        new_model = f"anthropic/{v}"
                    else:
                        new_model = original_model  # Keep the original model as-is

                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model} (CLAUDE)")
                    v = new_model
                else:
                    # Use OpenAI model
                    new_model = f"openai/{BIG_MODEL}"
                    logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")
                    v = new_model

            # Keep the model as is but add openai/ prefix if not already present
            elif not v.startswith('openai/') and not v.startswith('anthropic/'):
                new_model = f"openai/{v}"
                logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")
                v = new_model

            # Store the original model in the values dictionary
            values = info.data
            if isinstance(values, dict):
                values['original_model'] = original_model

            return v
        else:
            # Original behavior - ensure anthropic/ prefix
            if not v.startswith('anthropic/'):
                new_model = f"anthropic/{v}"
                logger.debug(f"📌 MODEL MAPPING: {original_model} ➡️ {new_model}")

                # Store original model
                values = info.data
                if isinstance(values, dict):
                    values['original_model'] = original_model

                return new_model
            return v

class TokenCountResponse(BaseModel):
    input_tokens: int

class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

class MessagesResponse(BaseModel):
    id: str
    model: str
    role: Literal["assistant"] = "assistant"
    content: List[Union[ContentBlockText, ContentBlockToolUse]]
    type: Literal["message"] = "message"
    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]] = None
    stop_sequence: Optional[str] = None
    usage: Usage

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Get request details
    method = request.method
    path = request.url.path

    # Log only basic request details at debug level
    logger.debug(f"Request: {method} {path}")

    # Process the request and get the response
    response = await call_next(request)

    return response

# Not using validation function as we're using the environment API key

def parse_tool_result_content(content):
    """Helper function to properly parse and normalize tool result content."""
    if content is None:
        return "No content provided"

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        result = ""
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                result += item.get("text", "") + "\n"
            elif isinstance(item, str):
                result += item + "\n"
            elif isinstance(item, dict):
                if "text" in item:
                    result += item.get("text", "") + "\n"
                else:
                    try:
                        result += json.dumps(item) + "\n"
                    except:
                        result += str(item) + "\n"
            else:
                try:
                    result += str(item) + "\n"
                except:
                    result += "Unparseable content\n"
        return result.strip()

    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text", "")
        try:
            return json.dumps(content)
        except:
            return str(content)

    # Fallback for any other type
    try:
        return str(content)
    except:
        return "Unparseable content"

def convert_anthropic_to_litellm(anthropic_request: MessagesRequest) -> Dict[str, Any]:
    """Convert Anthropic API request format to LiteLLM format (which follows OpenAI)."""
    # LiteLLM already handles Anthropic models when using the format model="anthropic/claude-3-opus-20240229"
    # So we just need to convert our Pydantic model to a dict in the expected format

    messages = []

    # Add system message if present
    if anthropic_request.system:
        # Handle different formats of system messages
        if isinstance(anthropic_request.system, str):
            # Simple string format
            messages.append({"role": "system", "content": anthropic_request.system})
        elif isinstance(anthropic_request.system, list):
            # List of content blocks
            system_text = ""
            for block in anthropic_request.system:
                if hasattr(block, 'type') and block.type == "text":
                    system_text += block.text + "\n\n"
                elif isinstance(block, dict) and block.get("type") == "text":
                    system_text += block.get("text", "") + "\n\n"

            if system_text:
                messages.append({"role": "system", "content": system_text.strip()})

    # Add conversation messages
    for idx, msg in enumerate(anthropic_request.messages):
        content = msg.content
        if isinstance(content, str):
            messages.append({"role": msg.role, "content": content})
        else:
            # Special handling for tool_result in user messages
            # OpenAI/LiteLLM format expects the assistant to call the tool,
            # and the user's next message to include the result as plain text
            if msg.role == "user" and any(block.type == "tool_result" for block in content if hasattr(block, "type")):
                # For user messages with tool_result, split into separate messages
                text_content = ""

                # Extract all text parts and concatenate them
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text_content += block.text + "\n"
                        elif block.type == "tool_result":
                            # Add tool result as a message by itself - simulate the normal flow
                            tool_id = block.tool_use_id if hasattr(block, "tool_use_id") else ""

                            # Handle different formats of tool result content
                            result_content = ""
                            if hasattr(block, "content"):
                                if isinstance(block.content, str):
                                    result_content = block.content
                                elif isinstance(block.content, list):
                                    # If content is a list of blocks, extract text from each
                                    for content_block in block.content:
                                        if hasattr(content_block, "type") and content_block.type == "text":
                                            result_content += content_block.text + "\n"
                                        elif isinstance(content_block, dict) and content_block.get("type") == "text":
                                            result_content += content_block.get("text", "") + "\n"
                                        elif isinstance(content_block, dict):
                                            # Handle any dict by trying to extract text or convert to JSON
                                            if "text" in content_block:
                                                result_content += content_block.get("text", "") + "\n"
                                            else:
                                                try:
                                                    result_content += json.dumps(content_block) + "\n"
                                                except:
                                                    result_content += str(content_block) + "\n"
                                elif isinstance(block.content, dict):
                                    # Handle dictionary content
                                    if block.content.get("type") == "text":
                                        result_content = block.content.get("text", "")
                                    else:
                                        try:
                                            result_content = json.dumps(block.content)
                                        except:
                                            result_content = str(block.content)
                                else:
                                    # Handle any other type by converting to string
                                    try:
                                        result_content = str(block.content)
                                    except:
                                        result_content = "Unparseable content"

                            # In OpenAI format, tool results come from the user (rather than being content blocks)
                            text_content += f"Tool result for {tool_id}:\n{result_content}\n"

                # Add as a single user message with all the content
                messages.append({"role": "user", "content": text_content.strip()})
            else:
                # Regular handling for other message types
                processed_content = []
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            processed_content.append({"type": "text", "text": block.text})
                        elif block.type == "image":
                            processed_content.append({"type": "image", "source": block.source})
                        elif block.type == "tool_use":
                            # Handle tool use blocks if needed
                            processed_content.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input
                            })
                        elif block.type == "tool_result":
                            # Handle different formats of tool result content
                            processed_content_block = {
                                "type": "tool_result",
                                "tool_use_id": block.tool_use_id if hasattr(block, "tool_use_id") else ""
                            }

                            # Process the content field properly
                            if hasattr(block, "content"):
                                if isinstance(block.content, str):
                                    # If it's a simple string, create a text block for it
                                    processed_content_block["content"] = [{"type": "text", "text": block.content}]
                                elif isinstance(block.content, list):
                                    # If it's already a list of blocks, keep it
                                    processed_content_block["content"] = block.content
                                else:
                                    # Default fallback
                                    processed_content_block["content"] = [{"type": "text", "text": str(block.content)}]
                            else:
                                # Default empty content
                                processed_content_block["content"] = [{"type": "text", "text": ""}]

                            processed_content.append(processed_content_block)

                messages.append({"role": msg.role, "content": processed_content})

    # Cap max_tokens for OpenAI models to their limit of 16384
    max_tokens = anthropic_request.max_tokens
    if anthropic_request.model.startswith("openai/") or USE_OPENAI_MODELS:
        max_tokens = min(max_tokens, 16384)
        logger.debug(f"Capping max_tokens to 16384 for OpenAI model (original value: {anthropic_request.max_tokens})")

    # Create LiteLLM request dict
    litellm_request = {
        "model": anthropic_request.model,  # t understands "anthropic/claude-x" format
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": anthropic_request.temperature,
        "stream": anthropic_request.stream,
    }

    # Extract the clean model name for feature checks
    clean_model = anthropic_request.model
    if clean_model.startswith("anthropic/"):
        clean_model = clean_model[len("anthropic/"):]
    elif clean_model.startswith("openai/"):
        clean_model = clean_model[len("openai/"):]

    # Add reasoning_effort parameter if provided in the request
    if anthropic_request.reasoning_effort:
        litellm_request["reasoning_effort"] = anthropic_request.reasoning_effort
        logger.debug(f"Using reasoning_effort={anthropic_request.reasoning_effort} from request")
    # Otherwise, add default reasoning_effort for OpenAI reasoning models
    elif "o3-" in clean_model or "o1" in clean_model:
        litellm_request["reasoning_effort"] = "high"  # Default to high
        logger.debug(f"Adding default reasoning_effort=high for reasoning model: {clean_model}")

    # Add optional parameters if present
    if anthropic_request.stop_sequences:
        litellm_request["stop"] = anthropic_request.stop_sequences

    if anthropic_request.top_p:
        litellm_request["top_p"] = anthropic_request.top_p

    if anthropic_request.top_k:
        litellm_request["top_k"] = anthropic_request.top_k

    # Convert tools to OpenAI format
    if anthropic_request.tools:
        openai_tools = []
        for tool in anthropic_request.tools:
            # Convert to dict if it's a pydantic model
            if hasattr(tool, 'dict'):
                tool_dict = tool.dict()
            else:
                tool_dict = tool

            # Create OpenAI-compatible function tool
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool_dict["name"],
                    "description": tool_dict.get("description", ""),
                    "parameters": tool_dict["input_schema"]
                }
            }
            openai_tools.append(openai_tool)

        litellm_request["tools"] = openai_tools

    # Convert tool_choice to OpenAI format if present
    if anthropic_request.tool_choice:
        if hasattr(anthropic_request.tool_choice, 'dict'):
            tool_choice_dict = anthropic_request.tool_choice.dict()
        else:
            tool_choice_dict = anthropic_request.tool_choice

        # Handle Anthropic's tool_choice format
        choice_type = tool_choice_dict.get("type")
        if choice_type == "auto":
            litellm_request["tool_choice"] = "auto"
        elif choice_type == "any":
            litellm_request["tool_choice"] = "any"
        elif choice_type == "tool" and "name" in tool_choice_dict:
            litellm_request["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice_dict["name"]}
            }
        else:
            # Default to auto if we can't determine
            litellm_request["tool_choice"] = "auto"

    return litellm_request

def convert_litellm_to_anthropic(litellm_response: Union[Dict[str, Any], Any],
                                 original_request: MessagesRequest) -> MessagesResponse:
    """Convert LiteLLM (OpenAI format) response to Anthropic API response format."""

    # Enhanced response extraction with better error handling
    try:
        # Get the clean model name to check capabilities
        clean_model = original_request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/"):]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/"):]

        # Check if this is a Claude model (which supports content blocks)
        is_claude_model = clean_model.startswith("claude-")

        # Handle ModelResponse object from LiteLLM
        if hasattr(litellm_response, 'choices') and hasattr(litellm_response, 'usage'):
            # Extract data from ModelResponse object directly
            choices = litellm_response.choices
            message = choices[0].message if choices and len(choices) > 0 else None
            content_text = message.content if message and hasattr(message, 'content') else ""
            tool_calls = message.tool_calls if message and hasattr(message, 'tool_calls') else None
            finish_reason = choices[0].finish_reason if choices and len(choices) > 0 else "stop"
            usage_info = litellm_response.usage
            response_id = getattr(litellm_response, 'id', f"msg_{uuid.uuid4()}")
        else:
            # For backward compatibility - handle dict responses
            # If response is a dict, use it, otherwise try to convert to dict
            try:
                response_dict = litellm_response if isinstance(litellm_response, dict) else litellm_response.dict()
            except AttributeError:
                # If .dict() fails, try to use model_dump or __dict__
                try:
                    response_dict = litellm_response.model_dump() if hasattr(litellm_response, 'model_dump') else litellm_response.__dict__
                except AttributeError:
                    # Fallback - manually extract attributes
                    response_dict = {
                        "id": getattr(litellm_response, 'id', f"msg_{uuid.uuid4()}"),
                        "choices": getattr(litellm_response, 'choices', [{}]),
                        "usage": getattr(litellm_response, 'usage', {})
                    }

            # Extract the content from the response dict
            choices = response_dict.get("choices", [{}])
            message = choices[0].get("message", {})
            content_text = message.get("content", "")
            tool_calls = message.get("tool_calls", None)
            finish_reason = choices[0].get("finish_reason", "stop") if choices and len(choices) > 0 else "stop"
            usage_info = response_dict.get("usage", {})
            response_id = response_dict.get("id", f"msg_{uuid.uuid4()}")

        # Create content list for Anthropic format
        content = []

        # Add text content block if present (text might be None or empty for pure tool call responses)
        if content_text is not None and content_text != "":
            content.append({"type": "text", "text": content_text})

        # Add tool calls if present (tool_use in Anthropic format) - only for Claude models
        if tool_calls and is_claude_model:
            logger.debug(f"Processing tool calls: {tool_calls}")

            # Convert to list if it's not already
            if not isinstance(tool_calls, list):
                tool_calls = [tool_calls]

            for idx, tool_call in enumerate(tool_calls):
                logger.debug(f"Processing tool call {idx}: {tool_call}")

                # Extract function data based on whether it's a dict or object
                if isinstance(tool_call, dict):
                    function = tool_call.get("function", {})
                    tool_id = tool_call.get("id", f"tool_{uuid.uuid4()}")
                    name = function.get("name", "")
                    arguments = function.get("arguments", "{}")
                else:
                    function = getattr(tool_call, "function", None)
                    tool_id = getattr(tool_call, "id", f"tool_{uuid.uuid4()}")
                    name = getattr(function, "name", "") if function else ""
                    arguments = getattr(function, "arguments", "{}") if function else "{}"

                # Convert string arguments to dict if needed
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse tool arguments as JSON: {arguments}")
                        arguments = {"raw": arguments}

                logger.debug(f"Adding tool_use block: id={tool_id}, name={name}, input={arguments}")

                content.append({
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": arguments
                })
        elif tool_calls and not is_claude_model:
            # For non-Claude models, convert tool calls to text format
            logger.debug(f"Converting tool calls to text for non-Claude model: {clean_model}")

            # We'll append tool info to the text content
            tool_text = "\n\nTool usage:\n"

            # Convert to list if it's not already
            if not isinstance(tool_calls, list):
                tool_calls = [tool_calls]

            for idx, tool_call in enumerate(tool_calls):
                # Extract function data based on whether it's a dict or object
                if isinstance(tool_call, dict):
                    function = tool_call.get("function", {})
                    tool_id = tool_call.get("id", f"tool_{uuid.uuid4()}")
                    name = function.get("name", "")
                    arguments = function.get("arguments", "{}")
                else:
                    function = getattr(tool_call, "function", None)
                    tool_id = getattr(tool_call, "id", f"tool_{uuid.uuid4()}")
                    name = getattr(function, "name", "") if function else ""
                    arguments = getattr(function, "arguments", "{}") if function else "{}"

                # Convert string arguments to dict if needed
                if isinstance(arguments, str):
                    try:
                        args_dict = json.loads(arguments)
                        arguments_str = json.dumps(args_dict, indent=2)
                    except json.JSONDecodeError:
                        arguments_str = arguments
                else:
                    arguments_str = json.dumps(arguments, indent=2)

                tool_text += f"Tool: {name}\nArguments: {arguments_str}\n\n"

            # Add or append tool text to content
            if content and content[0]["type"] == "text":
                content[0]["text"] += tool_text
            else:
                content.append({"type": "text", "text": tool_text})

        # Get usage information - extract values safely from object or dict
        if isinstance(usage_info, dict):
            prompt_tokens = usage_info.get("prompt_tokens", 0)
            completion_tokens = usage_info.get("completion_tokens", 0)
        else:
            prompt_tokens = getattr(usage_info, "prompt_tokens", 0)
            completion_tokens = getattr(usage_info, "completion_tokens", 0)

        # Map OpenAI finish_reason to Anthropic stop_reason
        stop_reason = None
        if finish_reason == "stop":
            stop_reason = "end_turn"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"  # Default

        # Make sure content is never empty
        if not content:
            content.append({"type": "text", "text": ""})

        # Create Anthropic-style response
        anthropic_response = MessagesResponse(
            id=response_id,
            model=original_request.model,
            role="assistant",
            content=content,
            stop_reason=stop_reason,
            stop_sequence=None,
            usage=Usage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens
            )
        )

        return anthropic_response

    except Exception as e:
        global REQUEST_HISTORY
        error_traceback = traceback.format_exc()

        # Record error in history
        request_info = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "original_model": original_model if 'original_model' in locals() else "unknown",
            "mapped_model": "",
            "num_messages": 0,
            "num_tools": 0,
            "stream": False,
            "status": "error",
            "error": str(e)
        }

        REQUEST_HISTORY.insert(0, request_info)
        if len(REQUEST_HISTORY) > MAX_HISTORY:
            REQUEST_HISTORY = REQUEST_HISTORY[:MAX_HISTORY]

        # Capture as much info as possible about the error
        error_details = {
            "error": str(e),
            "type": type(e).__name__,
            "traceback": error_traceback
        }

        # Check for LiteLLM-specific attributes
        for attr in ['message', 'status_code', 'response', 'llm_provider', 'model']:
            if hasattr(e, attr):
                error_details[attr] = getattr(e, attr)

        # Check for additional exception details in dictionaries
        if hasattr(e, '__dict__'):
            for key, value in e.__dict__.items():
                if key not in error_details and key not in ['args', '__traceback__']:
                    # Make sure values are JSON serializable
                    try:
                        json.dumps({key: value})  # Test if serializable
                        error_details[key] = value
                    except (TypeError, OverflowError):
                        # Handle non-serializable objects by converting to string
                        error_details[key] = str(value)

        # Log all error details
        try:
            logger.error(f"Error processing request: {json.dumps(error_details, indent=2)}")
        except (TypeError, OverflowError):
            # Fallback if json serialization fails
            logger.error(f"Error processing request (raw): {error_details}")

        # Format error for response with more user-friendly messages
        error_str = str(e).lower()

        # Check for specific error cases and provide more user-friendly messages
        if "overloaded" in error_str:
            user_message = "Anthropic API is currently overloaded. Please try again in a few minutes."
        elif "rate limit" in error_str or "rate_limit" in error_str or "429" in error_str:
            user_message = "Rate limit exceeded. Please try again in a few minutes."
        elif "timeout" in error_str or "timed out" in error_str:
            user_message = "The request timed out. The API server may be experiencing high load. Please try again."
        elif "connectivity" in error_str or "connection" in error_str:
            user_message = "Connection issue detected. Please check your internet connection and try again."
        elif "auth" in error_str or "authentication" in error_str or "key" in error_str and "invalid" in error_str:
            user_message = "Authentication error. Please check your API key configuration."
        else:
            # Default error message with details
            user_message = f"Error: {str(e)}"
            if 'message' in error_details and error_details['message']:
                user_message += f"\nMessage: {error_details['message']}"
            if 'response' in error_details and error_details['response']:
                user_message += f"\nResponse: {error_details['response']}"

        # Return detailed error
        status_code = error_details.get('status_code', 500)
        raise HTTPException(status_code=status_code, detail=user_message)

async def handle_streaming(response_generator, original_request: MessagesRequest):
    """Handle streaming responses from LiteLLM and convert to Anthropic format."""
    try:
        # Send message_start event
        message_id = f"msg_{uuid.uuid4().hex[:24]}"  # Format similar to Anthropic's IDs

        message_data = {
            'type': 'message_start',
            'message': {
                'id': message_id,
                'type': 'message',
                'role': 'assistant',
                'model': original_request.model,
                'content': [],
                'stop_reason': None,
                'stop_sequence': None,
                'usage': {
                    'input_tokens': 0,
                    'cache_creation_input_tokens': 0,
                    'cache_read_input_tokens': 0,
                    'output_tokens': 0
                }
            }
        }
        yield f"event: message_start\ndata: {json.dumps(message_data)}\n\n"

        # Content block index for the first text block
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

        # Send a ping to keep the connection alive (Anthropic does this)
        yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"

        tool_index = None
        current_tool_call = None
        tool_content = ""
        accumulated_text = ""  # Track accumulated text content
        text_sent = False  # Track if we've sent any text content
        text_block_closed = False  # Track if text block is closed
        input_tokens = 0
        output_tokens = 0
        has_sent_stop_reason = False
        last_tool_index = 0

        # Process each chunk
        async for chunk in response_generator:
            try:


                # Check if this is the end of the response with usage data
                if hasattr(chunk, 'usage') and chunk.usage is not None:
                    if hasattr(chunk.usage, 'prompt_tokens'):
                        input_tokens = chunk.usage.prompt_tokens
                    if hasattr(chunk.usage, 'completion_tokens'):
                        output_tokens = chunk.usage.completion_tokens

                # Handle text content
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    choice = chunk.choices[0]

                    # Get the delta from the choice
                    if hasattr(choice, 'delta'):
                        delta = choice.delta
                    else:
                        # If no delta, try to get message
                        delta = getattr(choice, 'message', {})

                    # Check for finish_reason to know when we're done
                    finish_reason = getattr(choice, 'finish_reason', None)

                    # Process text content
                    delta_content = None

                    # Handle different formats of delta content
                    if hasattr(delta, 'content'):
                        delta_content = delta.content
                    elif isinstance(delta, dict) and 'content' in delta:
                        delta_content = delta['content']

                    # Accumulate text content
                    if delta_content is not None and delta_content != "":
                        accumulated_text += delta_content

                        # Always emit text deltas if no tool calls started
                        if tool_index is None and not text_block_closed:
                            text_sent = True
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': delta_content}})}\n\n"

                    # Process tool calls
                    delta_tool_calls = None

                    # Handle different formats of tool calls
                    if hasattr(delta, 'tool_calls'):
                        delta_tool_calls = delta.tool_calls
                    elif isinstance(delta, dict) and 'tool_calls' in delta:
                        delta_tool_calls = delta['tool_calls']

                    # Process tool calls if any
                    if delta_tool_calls:
                        # First tool call we've seen - need to handle text properly
                        if tool_index is None:
                            # If we've been streaming text, close that text block
                            if text_sent and not text_block_closed:
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                            # If we've accumulated text but not sent it, we need to emit it now
                            # This handles the case where the first delta has both text and a tool call
                            elif accumulated_text and not text_sent and not text_block_closed:
                                # Send the accumulated text
                                text_sent = True
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': accumulated_text}})}\n\n"
                                # Close the text block
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                            # Close text block even if we haven't sent anything - models sometimes emit empty text blocks
                            elif not text_block_closed:
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

                        # Convert to list if it's not already
                        if not isinstance(delta_tool_calls, list):
                            delta_tool_calls = [delta_tool_calls]

                        for tool_call in delta_tool_calls:
                            # Get the index of this tool call (for multiple tools)
                            current_index = None
                            if isinstance(tool_call, dict) and 'index' in tool_call:
                                current_index = tool_call['index']
                            elif hasattr(tool_call, 'index'):
                                current_index = tool_call.index
                            else:
                                current_index = 0

                            # Check if this is a new tool or a continuation
                            if tool_index is None or current_index != tool_index:
                                # New tool call - create a new tool_use block
                                tool_index = current_index
                                last_tool_index += 1
                                anthropic_tool_index = last_tool_index

                                # Extract function info
                                if isinstance(tool_call, dict):
                                    function = tool_call.get('function', {})
                                    name = function.get('name', '') if isinstance(function, dict) else ""
                                    tool_id = tool_call.get('id', f"toolu_{uuid.uuid4().hex[:24]}")
                                else:
                                    function = getattr(tool_call, 'function', None)
                                    name = getattr(function, 'name', '') if function else ''
                                    tool_id = getattr(tool_call, 'id', f"toolu_{uuid.uuid4().hex[:24]}")

                                # Start a new tool_use block
                                tool_block_data = {"type": "content_block_start", "index": anthropic_tool_index, "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}}
                                yield f"event: content_block_start\ndata: {json.dumps(tool_block_data)}\n\n"
                                current_tool_call = tool_call
                                tool_content = ""

                            # Extract function arguments
                            arguments = None
                            if isinstance(tool_call, dict) and 'function' in tool_call:
                                function = tool_call.get('function', {})
                                arguments = function.get('arguments', '') if isinstance(function, dict) else ''
                            elif hasattr(tool_call, 'function'):
                                function = getattr(tool_call, 'function', None)
                                arguments = getattr(function, 'arguments', '') if function else ''

                            # If we have arguments, send them as a delta
                            if arguments:
                                # Try to detect if arguments are valid JSON or just a fragment
                                try:
                                    # If it's already a dict, use it
                                    if isinstance(arguments, dict):
                                        args_json = json.dumps(arguments)
                                    else:
                                        # Otherwise, try to parse it
                                        json.loads(arguments)
                                        args_json = arguments
                                except (json.JSONDecodeError, TypeError):
                                    # If it's a fragment, treat it as a string
                                    args_json = arguments

                                # Add to accumulated tool content
                                tool_content += args_json if isinstance(args_json, str) else ""

                                # Send the update
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anthropic_tool_index, 'delta': {'type': 'input_json_delta', 'partial_json': args_json}})}\n\n"

                    # Process finish_reason - end the streaming response
                    if finish_reason and not has_sent_stop_reason:
                        has_sent_stop_reason = True

                        # Close any open tool call blocks
                        if tool_index is not None:
                            for i in range(1, last_tool_index + 1):
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"

                        # If we accumulated text but never sent or closed text block, do it now
                        if not text_block_closed:
                            if accumulated_text and not text_sent:
                                # Send the accumulated text
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': accumulated_text}})}\n\n"
                            # Close the text block
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

                        # Map OpenAI finish_reason to Anthropic stop_reason
                        stop_reason = "end_turn"
                        if finish_reason == "length":
                            stop_reason = "max_tokens"
                        elif finish_reason == "tool_calls":
                            stop_reason = "tool_use"
                        elif finish_reason == "stop":
                            stop_reason = "end_turn"

                        # Send message_delta with stop reason and usage
                        usage = {"output_tokens": output_tokens}

                        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': usage})}\n\n"

                        # Send message_stop event
                        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

                        # Send final [DONE] marker to match Anthropic's behavior
                        yield "data: [DONE]\n\n"

                        return
            except Exception as e:
                # Log error but continue processing other chunks
                logger.error(f"Error processing chunk: {str(e)}")
                continue

        # If we didn't get a finish reason, close any open blocks
        if not has_sent_stop_reason:
            # Close any open tool call blocks
            if tool_index is not None:
                for i in range(1, last_tool_index + 1):
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"

            # Close the text content block
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

            # Send final message_delta with usage
            usage = {"output_tokens": output_tokens}

            yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': usage})}\n\n"

            # Send message_stop event
            yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

            # Send final [DONE] marker to match Anthropic's behavior
            yield "data: [DONE]\n\n"

    except Exception as e:
        error_traceback = traceback.format_exc()

        # Determine a user-friendly error message
        error_str = str(e).lower()
        if "overloaded" in error_str:
            user_message = "Anthropic API is currently overloaded. Please try again in a few minutes."
        elif "rate limit" in error_str or "rate_limit" in error_str or "429" in error_str:
            user_message = "Rate limit exceeded. Please try again in a few minutes."
        elif "timeout" in error_str or "timed out" in error_str:
            user_message = "The request timed out. The API server may be experiencing high load. Please try again."
        elif "connectivity" in error_str or "connection" in error_str:
            user_message = "Connection issue detected. Please check your internet connection and try again."
        elif "auth" in error_str or "authentication" in error_str or "key" in error_str and "invalid" in error_str:
            user_message = "Authentication error. Please check your API key configuration."
        else:
            user_message = f"Error in streaming: {str(e)}"

        error_message = f"{user_message}\n\nFull traceback:\n{error_traceback}"
        logger.error(error_message)

        # Send error message_delta with user-friendly message
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'error', 'stop_sequence': None, 'error_message': user_message}, 'usage': {'output_tokens': 0}})}\n\n"

        # Send message_stop event
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

        # Send final [DONE] marker
        yield "data: [DONE]\n\n"

@app.post("/v1/messages")
async def create_message(
    request: MessagesRequest,
    raw_request: Request
):
    global REQUEST_HISTORY
    try:
        # print the body here
        body = await raw_request.body()

        # Parse the raw body as JSON since it's bytes
        body_json = json.loads(body.decode('utf-8'))
        original_model = body_json.get("model", "unknown")

        # Get the display name for logging, just the model name without provider prefix
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]

        # Clean model name for capability check
        clean_model = request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/"):]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/"):]

        logger.debug(f" PROCESSING REQUEST: Model={request.model}, Stream={request.stream}")

        # Convert Anthropic request to LiteLLM format
        litellm_request = convert_anthropic_to_litellm(request)

        # Store request in history for UI display
        request_info = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "original_model": original_model,
            "mapped_model": litellm_request.get('model', ''),
            "num_messages": len(litellm_request['messages']),
            "num_tools": len(request.tools) if request.tools else 0,
            "stream": request.stream,
            "status": "success"
        }

        # Keep history limited
        REQUEST_HISTORY.insert(0, request_info)
        if len(REQUEST_HISTORY) > MAX_HISTORY:
            REQUEST_HISTORY = REQUEST_HISTORY[:MAX_HISTORY]

        # Determine which API key to use based on the model
        if request.model.startswith("openai/"):
            if not OPENAI_API_KEY:
                raise HTTPException(status_code=401, detail="Missing OpenAI API key. Please set OPENAI_API_KEY in your environment variables.")
            litellm_request["api_key"] = OPENAI_API_KEY
        else:
            if not ANTHROPIC_API_KEY:
                raise HTTPException(status_code=401, detail="Missing Anthropic API key. Please set ANTHROPIC_API_KEY in your environment variables.")
            litellm_request["api_key"] = ANTHROPIC_API_KEY

        # For OpenAI models - modify request format to work with limitations
        if "openai" in litellm_request["model"] and "messages" in litellm_request:
            logger.debug(f"Processing OpenAI model request: {litellm_request['model']}")

            # For OpenAI models, we need to convert content blocks to simple strings
            # and handle other requirements
            for i, msg in enumerate(litellm_request["messages"]):
                # Special case - handle message content directly when it's a list of tool_result
                # This is a specific case we're seeing in the error
                if "content" in msg and isinstance(msg["content"], list):
                    is_only_tool_result = True
                    for block in msg["content"]:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            is_only_tool_result = False
                            break

                    if is_only_tool_result and len(msg["content"]) > 0:
                        logger.warning(f"Found message with only tool_result content - special handling required")
                        # Extract the content from all tool_result blocks
                        all_text = ""
                        for block in msg["content"]:
                            all_text += "Tool Result:\n"
                            result_content = block.get("content", [])

                            # Handle different formats of content
                            if isinstance(result_content, list):
                                for item in result_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        all_text += item.get("text", "") + "\n"
                                    elif isinstance(item, dict):
                                        # Fall back to string representation of any dict
                                        try:
                                            item_text = item.get("text", json.dumps(item))
                                            all_text += item_text + "\n"
                                        except:
                                            all_text += str(item) + "\n"
                            elif isinstance(result_content, str):
                                all_text += result_content + "\n"
                            else:
                                try:
                                    all_text += json.dumps(result_content) + "\n"
                                except:
                                    all_text += str(result_content) + "\n"

                        # Replace the list with extracted text
                        litellm_request["messages"][i]["content"] = all_text.strip() or "..."
                        logger.warning(f"Converted tool_result to plain text: {all_text.strip()[:200]}...")
                        continue  # Skip normal processing for this message

                # 1. Handle content field - normal case
                if "content" in msg:
                    # Check if content is a list (content blocks)
                    if isinstance(msg["content"], list):
                        # Convert complex content blocks to simple string
                        text_content = ""
                        for block in msg["content"]:
                            if isinstance(block, dict):
                                # Handle different content block types
                                if block.get("type") == "text":
                                    text_content += block.get("text", "") + "\n"

                                # Handle tool_result content blocks - extract nested text
                                elif block.get("type") == "tool_result":
                                    tool_id = block.get("tool_use_id", "unknown")
                                    text_content += f"[Tool Result ID: {tool_id}]\n"

                                    # Extract text from the tool_result content
                                    result_content = block.get("content", [])
                                    if isinstance(result_content, list):
                                        for item in result_content:
                                            if isinstance(item, dict) and item.get("type") == "text":
                                                text_content += item.get("text", "") + "\n"
                                            elif isinstance(item, dict):
                                                # Handle any dict by trying to extract text or convert to JSON
                                                if "text" in item:
                                                    text_content += item.get("text", "") + "\n"
                                                else:
                                                    try:
                                                        text_content += json.dumps(item) + "\n"
                                                    except:
                                                        text_content += str(item) + "\n"
                                    elif isinstance(result_content, dict):
                                        # Handle dictionary content
                                        if result_content.get("type") == "text":
                                            text_content += result_content.get("text", "") + "\n"
                                        else:
                                            try:
                                                text_content += json.dumps(result_content) + "\n"
                                            except:
                                                text_content += str(result_content) + "\n"
                                    elif isinstance(result_content, str):
                                        text_content += result_content + "\n"
                                    else:
                                        try:
                                            text_content += json.dumps(result_content) + "\n"
                                        except:
                                            text_content += str(result_content) + "\n"

                                # Handle tool_use content blocks
                                elif block.get("type") == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    tool_id = block.get("id", "unknown")
                                    tool_input = json.dumps(block.get("input", {}))
                                    text_content += f"[Tool: {tool_name} (ID: {tool_id})]\nInput: {tool_input}\n\n"

                                # Handle image content blocks
                                elif block.get("type") == "image":
                                    text_content += "[Image content - not displayed in text format]\n"

                        # Make sure content is never empty for OpenAI models
                        if not text_content.strip():
                            text_content = "..."

                        litellm_request["messages"][i]["content"] = text_content.strip()
                    # Also check for None or empty string content
                    elif msg["content"] is None:
                        litellm_request["messages"][i]["content"] = "..." # Empty content not allowed

                # 2. Remove any fields OpenAI doesn't support in messages
                for key in list(msg.keys()):
                    if key not in ["role", "content", "name", "tool_call_id", "tool_calls"]:
                        logger.warning(f"Removing unsupported field from message: {key}")
                        del msg[key]

            # 3. Final validation - check for any remaining invalid values and dump full message details
            for i, msg in enumerate(litellm_request["messages"]):
                # Log the message format for debugging
                logger.debug(f"Message {i} format check - role: {msg.get('role')}, content type: {type(msg.get('content'))}")

                # If content is still a list or None, replace with placeholder
                if isinstance(msg.get("content"), list):
                    logger.warning(f"CRITICAL: Message {i} still has list content after processing: {json.dumps(msg.get('content'))}")
                    # Last resort - stringify the entire content as JSON
                    litellm_request["messages"][i]["content"] = json.dumps(msg.get('content'))
                elif msg.get("content") is None:
                    logger.warning(f"Message {i} has None content - replacing with placeholder")
                    litellm_request["messages"][i]["content"] = "..." # Fallback placeholder

        # Only log basic info about the request, not the full details
        logger.debug(f"Request for model: {litellm_request.get('model')}, stream: {litellm_request.get('stream', False)}")

        # Handle streaming mode
        if request.stream:
            # Use LiteLLM for streaming
            num_tools = len(request.tools) if request.tools else 0

            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                litellm_request.get('model'),
                len(litellm_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            # Ensure we use the async version for streaming
            response_generator = await litellm.acompletion(**litellm_request)

            return StreamingResponse(
                handle_streaming(response_generator, request),
                media_type="text/event-stream"
            )
        else:
            # Use LiteLLM for regular completion
            num_tools = len(request.tools) if request.tools else 0

            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                litellm_request.get('model'),
                len(litellm_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            start_time = time.time()
            litellm_response = litellm.completion(**litellm_request)
            logger.debug(f"✅ RESPONSE RECEIVED: Model={litellm_request.get('model')}, Time={time.time() - start_time:.2f}s")

            # Convert LiteLLM response to Anthropic format
            anthropic_response = convert_litellm_to_anthropic(litellm_response, request)

            return anthropic_response

    except Exception as e:
        error_traceback = traceback.format_exc()

        # Record error in history
        request_info = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "original_model": original_model if 'original_model' in locals() else "unknown",
            "mapped_model": "",
            "num_messages": 0,
            "num_tools": 0,
            "stream": False,
            "status": "error",
            "error": str(e)
        }

        REQUEST_HISTORY.insert(0, request_info)
        if len(REQUEST_HISTORY) > MAX_HISTORY:
            REQUEST_HISTORY = REQUEST_HISTORY[:MAX_HISTORY]

        # Capture as much info as possible about the error
        error_details = {
            "error": str(e),
            "type": type(e).__name__,
            "traceback": error_traceback
        }

        # Check for LiteLLM-specific attributes
        for attr in ['message', 'status_code', 'response', 'llm_provider', 'model']:
            if hasattr(e, attr):
                error_details[attr] = getattr(e, attr)

        # Check for additional exception details in dictionaries
        if hasattr(e, '__dict__'):
            for key, value in e.__dict__.items():
                if key not in error_details and key not in ['args', '__traceback__']:
                    # Make sure values are JSON serializable
                    try:
                        json.dumps({key: value})  # Test if serializable
                        error_details[key] = value
                    except (TypeError, OverflowError):
                        # Handle non-serializable objects by converting to string
                        error_details[key] = str(value)

        # Log all error details
        try:
            logger.error(f"Error processing request: {json.dumps(error_details, indent=2)}")
        except (TypeError, OverflowError):
            # Fallback if json serialization fails
            logger.error(f"Error processing request (raw): {error_details}")

        # Format error for response with more user-friendly messages
        error_str = str(e).lower()

        # Check for specific error cases and provide more user-friendly messages
        if "overloaded" in error_str:
            user_message = "Anthropic API is currently overloaded. Please try again in a few minutes."
        elif "rate limit" in error_str or "rate_limit" in error_str or "429" in error_str:
            user_message = "Rate limit exceeded. Please try again in a few minutes."
        elif "timeout" in error_str or "timed out" in error_str:
            user_message = "The request timed out. The API server may be experiencing high load. Please try again."
        elif "connectivity" in error_str or "connection" in error_str:
            user_message = "Connection issue detected. Please check your internet connection and try again."
        elif "auth" in error_str or "authentication" in error_str or "key" in error_str and "invalid" in error_str:
            user_message = "Authentication error. Please check your API key configuration."
        else:
            # Default error message with details
            user_message = f"Error: {str(e)}"
            if 'message' in error_details and error_details['message']:
                user_message += f"\nMessage: {error_details['message']}"
            if 'response' in error_details and error_details['response']:
                user_message += f"\nResponse: {error_details['response']}"

        # Return detailed error
        status_code = error_details.get('status_code', 500)
        raise HTTPException(status_code=status_code, detail=user_message)

@app.post("/v1/messages/count_tokens")
async def count_tokens(
    request: TokenCountRequest,
    raw_request: Request
):
    try:
        # Log the incoming token count request
        original_model = request.original_model or request.model

        # Get the display name for logging, just the model name without provider prefix
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]

        # Clean model name for capability check
        clean_model = request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/"):]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/"):]

        # Convert the messages to a format LiteLLM can understand
        converted_request = convert_anthropic_to_litellm(
            MessagesRequest(
                model=request.model,
                max_tokens=100,  # Arbitrary value not used for token counting
                messages=request.messages,
                system=request.system,
                tools=request.tools,
                tool_choice=request.tool_choice,
                thinking=request.thinking
            )
        )

        # Use LiteLLM's token_counter function
        try:
            # Import token_counter function
            from litellm import token_counter

            # Log the request beautifully
            num_tools = len(request.tools) if request.tools else 0

            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                converted_request.get('model'),
                len(converted_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )

            # Count tokens
            token_count = token_counter(
                model=converted_request["model"],
                messages=converted_request["messages"],
            )

            # Return Anthropic-style response
            return TokenCountResponse(input_tokens=token_count)

        except ImportError:
            logger.error("Could not import token_counter from litellm")
            # Fallback to a simple approximation
            return TokenCountResponse(input_tokens=1000)  # Default fallback

    except Exception as e:
        error_traceback = traceback.format_exc()
        logger.error(f"Error counting tokens: {str(e)}\n{error_traceback}")
        raise HTTPException(status_code=500, detail=f"Error counting tokens: {str(e)}")

@app.get("/", response_class=HTMLResponse)
async def ui_root(request: Request):
    # Get available models with display labels for the dropdown
    available_models = [
        # OpenAI models
        {"value": "gpt-4o", "label": "gpt-4o"},
        {"value": "gpt-4o-mini", "label": "gpt-4o-mini"},
        {"value": "gpt-3.5-turbo", "label": "gpt-3.5-turbo"},

        # OpenAI reasoning models
        {"value": "o1", "label": "o1"},
        {"value": "o3-mini", "label": "o3-mini"},

        # Anthropic models
        {"value": "claude-3-7-sonnet-20240229", "label": "claude-3-7-sonnet-20240229"},
        {"value": "claude-3-haiku-20240307", "label": "claude-3-haiku-20240307"},
    ]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "big_model": BIG_MODEL,
            "small_model": SMALL_MODEL,
            "available_models": available_models,
            "request_history": REQUEST_HISTORY
        }
    )

@app.post("/update_models")
async def update_models(big_model: str = Form(...), small_model: str = Form(...)):
    global BIG_MODEL, SMALL_MODEL, USE_OPENAI_MODELS

    # Check for appropriate API keys
    if ("claude" in big_model.lower() or "claude" in small_model.lower()) and not ANTHROPIC_API_KEY:
        return {"status": "error", "message": "Missing Anthropic API key. Please set ANTHROPIC_API_KEY in your environment variables."}

    if (big_model.startswith("openai/") or small_model.startswith("openai/") or
        (not big_model.startswith("anthropic/")) or
        (not small_model.startswith("anthropic/"))) and not OPENAI_API_KEY:
        return {"status": "error", "message": "Missing OpenAI API key. Please set OPENAI_API_KEY in your environment variables."}

    # Update the model settings
    BIG_MODEL = big_model
    SMALL_MODEL = small_model

    # Refresh environment - this is important for the model swap to take effect
    if "claude" in BIG_MODEL.lower() and "claude" in SMALL_MODEL.lower():
        USE_OPENAI_MODELS = False
        logger.debug(f"Using Claude models exclusively - disabling OpenAI model swapping")
    else:
        USE_OPENAI_MODELS = True
        logger.debug(f"Using non-Claude models - enabling model swapping")

    logger.warning(f"MODEL CONFIGURATION UPDATED: Big Model = {BIG_MODEL}, Small Model = {SMALL_MODEL}, USE_OPENAI_MODELS = {USE_OPENAI_MODELS}")

    return {"status": "success", "big_model": BIG_MODEL, "small_model": SMALL_MODEL, "use_openai_models": USE_OPENAI_MODELS}

@app.get("/api/history")
async def get_history():
    return {"history": REQUEST_HISTORY}

# Create the HTML template for the UI
@app.on_event("startup")
async def create_templates():
    # Create templates directory if it doesn't exist
    os.makedirs("templates", exist_ok=True)

    # Minimal server startup message with styling
    print(f"\n{Colors.GREEN}{Colors.BOLD}SERVER STARTED SUCCESSFULLY!{Colors.RESET}")
    print(f"{Colors.CYAN}Access the web UI at: {Colors.BOLD}http://localhost:8082{Colors.RESET}")
    print(f"{Colors.CYAN}Connect Claude Code with: {Colors.BOLD}ANTHROPIC_BASE_URL=http://localhost:8082 claude{Colors.RESET}\n")

    # Create index.html template
    index_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>OpenAI Code</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                padding-top: 2rem;
                background-color: #f0f2f5;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            }
            .header {
                background: linear-gradient(90deg, #10a37f, #0d8a6f);
                color: white;
                padding: 1.5rem;
                border-radius: 10px;
                margin-bottom: 2rem;
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            }
            .card {
                margin-bottom: 1.5rem;
                box-shadow: 0 6px 16px rgba(0,0,0,0.08);
                border: none;
                border-radius: 10px;
                transition: transform 0.2s ease, box-shadow 0.2s ease;
            }
            .card:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(0,0,0,0.12);
            }
            .card-header {
                background-color: #10a37f;
                color: white;
                font-weight: bold;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                padding: 1rem 1.25rem;
            }
            .model-badge {
                font-size: 0.85rem;
                padding: 0.35rem 0.75rem;
                border-radius: 20px;
                font-weight: 500;
            }
            .config-icon {
                font-size: 1.5rem;
                margin-right: 0.75rem;
            }
            .table-responsive {
                max-height: 500px;
                overflow-y: auto;
                border-radius: 8px;
            }
            .reasoning-note {
                font-size: 0.9rem;
                padding: 0.75rem;
                background-color: #e9f7f2;
                border-radius: 8px;
                border-left: 4px solid #10a37f;
                margin-bottom: 1.25rem;
            }
            .status-success {
                color: #10a37f;
                font-weight: 500;
            }
            .status-error {
                color: #dc3545;
                font-weight: 500;
            }
            .refresh-btn {
                font-size: 0.85rem;
                margin-left: 0.5rem;
                background-color: transparent;
                border-color: white;
            }
            .refresh-btn:hover {
                background-color: rgba(255,255,255,0.2);
                border-color: white;
            }
            .btn-primary {
                background-color: #10a37f;
                border-color: #10a37f;
            }
            .btn-primary:hover {
                background-color: #0d8a6f;
                border-color: #0d8a6f;
            }
            .list-group-item {
                border-radius: 6px;
                margin-bottom: 0.5rem;
            }
            pre {
                background-color: #f8f9fa;
                padding: 1rem;
                border-radius: 8px;
                border-left: 4px solid #10a37f;
            }
            .badge {
                font-weight: 500;
            }
            .bg-primary {
                background-color: #10a37f !important;
            }
            table {
                border-collapse: separate;
                border-spacing: 0;
            }
            table th:first-child {
                border-top-left-radius: 8px;
            }
            table th:last-child {
                border-top-right-radius: 8px;
            }
            .history-row-success {
                background-color: rgba(16, 163, 127, 0.05);
            }
            .history-row-success:hover {
                background-color: rgba(16, 163, 127, 0.1);
            }
            .history-row-error {
                background-color: rgba(220, 53, 69, 0.05);
            }
            .history-row-error:hover {
                background-color: rgba(220, 53, 69, 0.1);
            }
            .model-name {
                font-weight: 500;
                padding: 2px 6px;
                border-radius: 4px;
                display: inline-block;
            }
            .model-claude {
                color: #FF6B00;
            }
            .model-openai {
                color: #000000;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header text-center">
                <h1>OpenAI Code</h1>
                <p class="mb-0">Use OpenAI models with Cursor's Claude Code feature</p>
            </div>

            <!-- Error alert container - will be populated by JavaScript when errors occur -->
            <div id="errorContainer" class="mb-4"></div>

            <div class="row">
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header d-flex align-items-center">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-gear-fill config-icon" viewBox="0 0 16 16">
                                <path d="M9.405 1.05c-.413-1.4-2.397-1.4-2.81 0l-.1.34a1.464 1.464 0 0 1-2.105.872l-.31-.17c-1.283-.698-2.686.705-1.987 1.987l.169.311c.446.82.023 1.841-.872 2.105l-.34.1c-1.4.413-1.4 2.397 0 2.81l.34.1a1.464 1.464 0 0 1 .872 2.105l-.17.31c-.698 1.283.705 2.686 1.987 1.987l.311-.169a1.464 1.464 0 0 1 2.105.872l.1.34c.413 1.4 2.397 1.4 2.81 0l.1-.34a1.464 1.464 0 0 1 2.105-.872l.31.17c1.283.698 2.686-.705 1.987-1.987l-.169-.311a1.464 1.464 0 0 1 .872-2.105l.34-.1c1.4-.413 1.4-2.397 0-2.81l-.34-.1a1.464 1.464 0 0 1-.872-2.105l.17-.31c.698-1.283-.705-2.686-1.987-1.987l-.311.169a1.464 1.464 0 0 1-2.105-.872l-.1-.34z"/>
                            </svg>
                            Configuration
                        </div>
                        <div class="card-body">
                            <form id="modelForm" action="/update_models" method="post">
                                <div class="mb-3">
                                    <label for="bigModel" class="form-label">Big Model (for Sonnet)</label>
                                    <select class="form-select" id="bigModel" name="big_model">
                                        {% for model in available_models %}
                                            <option value="{{ model.value }}" {% if model.value == big_model %}selected{% endif %}>{{ model.label }}</option>
                                        {% endfor %}
                                    </select>
                                </div>
                                <div class="mb-3">
                                    <label for="smallModel" class="form-label">Small Model (for Haiku)</label>
                                    <select class="form-select" id="smallModel" name="small_model">
                                        {% for model in available_models %}
                                            <option value="{{ model.value }}" {% if model.value == small_model %}selected{% endif %}>{{ model.label }}</option>
                                        {% endfor %}
                                    </select>
                                </div>
                                <div class="reasoning-note mb-3">
                                    <strong>Model Options:</strong>
                                    <ul class="mb-0 mt-2">
                                        <li><strong>claude-3-haiku/sonnet:</strong> Use the original Claude models (requires Anthropic API key)</li>
                                        <li><strong>OpenAI models:</strong> Use gpt-4o, gpt-4o-mini instead of Claude models</li>
                                        <li><strong>Reasoning models:</strong> When using o3-mini or o1, reasoning_effort="medium" is automatically added</li>
                                    </ul>
                                </div>
                                <p><i>Note: The proxy automatically adds reasoning_effort="high" for reasoning models (o3-mini, o1).</i></p>
                                <button type="submit" class="btn btn-primary">Save Configuration</button>
                            </form>
                        </div>
                    </div>

                    <div class="card">
                        <div class="card-header">
                            Connection Info
                        </div>
                        <div class="card-body">
                            <h5>How to connect:</h5>
                            <pre class="bg-light p-3 rounded">ANTHROPIC_BASE_URL=http://localhost:8082 claude</pre>
                            <p>Run this command in your terminal to connect to this proxy and use with Cursor.</p>

                            <h5 class="mt-3">Current Mapping:</h5>
                            <ul class="list-group">
                                <li class="list-group-item d-flex justify-content-between align-items-center">
                                    Claude Sonnet
                                    <span class="badge bg-primary rounded-pill model-badge">{{ big_model }}</span>
                                </li>
                                <li class="list-group-item d-flex justify-content-between align-items-center">
                                    Claude Haiku
                                    <span class="badge bg-primary rounded-pill model-badge">{{ small_model }}</span>
                                </li>
                            </ul>
                        </div>
                    </div>
                </div>

                <div class="col-md-6">
                    <div class="card">
                        <div class="card-header d-flex justify-content-between align-items-center">
                            <div>
                                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-activity config-icon" viewBox="0 0 16 16">
                                    <path fill-rule="evenodd" d="M6 2a.5.5 0 0 1 .47.33L10 12.036l1.53-4.208A.5.5 0 0 1 12 7.5h3.5a.5.5 0 0 1 0 1h-3.15l-1.88 5.17a.5.5 0 0 1-.94 0L6 3.964 4.47 8.171A.5.5 0 0 1 4 8.5H.5a.5.5 0 0 1 0-1h3.15l1.88-5.17A.5.5 0 0 1 6 2Z"/>
                                </svg>
                                Request History
                            </div>
                            <button id="refreshHistory" class="btn btn-sm btn-outline-light refresh-btn">
                                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-arrow-clockwise" viewBox="0 0 16 16">
                                    <path fill-rule="evenodd" d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2v1z"/>
                                    <path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/>
                                </svg>
                                Refresh
                            </button>
                        </div>
                        <div class="card-body">
                            <div class="table-responsive">
                                <table class="table table-hover" id="historyTable">
                                    <thead>
                                        <tr>
                                            <th>Time</th>
                                            <th>Original</th>
                                            <th>Mapped To</th>
                                            <th>Messages</th>
                                            <th>Status</th>
                                            <th>Error</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {% for req in request_history %}
                                        <tr class="{% if req.status == 'success' %}history-row-success{% else %}history-row-error{% endif %}">
                                            <td>{{ req.timestamp }}</td>
                                            <td><span class="model-name {% if 'claude' in req.original_model.lower() %}model-claude{% else %}model-openai{% endif %}">{{ req.original_model }}</span></td>
                                            <td><span class="model-name {% if 'claude' in req.mapped_model.lower() %}model-claude{% else %}model-openai{% endif %}">{{ req.mapped_model }}</span></td>
                                            <td>{{ req.num_messages }}</td>
                                            <td class="{% if req.status == 'success' %}status-success{% else %}status-error{% endif %}">
                                                {{ req.status }}
                                            </td>
                                            <td>
                                                {% if req.status == 'error' and req.error %}
                                                <span class="badge bg-danger">{{ req.error }}</span>
                                                {% endif %}
                                            </td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            // Form submission via AJAX
            document.getElementById('modelForm').addEventListener('submit', function(e) {
                e.preventDefault();

                const formData = new FormData(this);

                fetch('/update_models', {
                    method: 'POST',
                    body: formData
                })
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'success') {
                        // Update UI elements to reflect the change immediately
                        const bigModelBadge = document.querySelector('.list-group-item:nth-child(1) .badge');
                        const smallModelBadge = document.querySelector('.list-group-item:nth-child(2) .badge');

                        if (bigModelBadge) bigModelBadge.textContent = data.big_model;
                        if (smallModelBadge) smallModelBadge.textContent = data.small_model;

                        // Add class for Claude models to style them differently
                        if (bigModelBadge) {
                            if (data.big_model.toLowerCase().includes('claude')) {
                                bigModelBadge.classList.add('model-claude');
                                bigModelBadge.classList.remove('model-openai');
                            } else {
                                bigModelBadge.classList.add('model-openai');
                                bigModelBadge.classList.remove('model-claude');
                            }
                        }

                        if (smallModelBadge) {
                            if (data.small_model.toLowerCase().includes('claude')) {
                                smallModelBadge.classList.add('model-claude');
                                smallModelBadge.classList.remove('model-openai');
                            } else {
                                smallModelBadge.classList.add('model-openai');
                                smallModelBadge.classList.remove('model-claude');
                            }
                        }

                        // Show success message
                        const errorContainer = document.getElementById('errorContainer');
                        const successAlert = document.createElement('div');
                        successAlert.className = 'alert alert-success alert-dismissible fade show';
                        successAlert.innerHTML = `
                            <strong>Success!</strong> Model configuration updated.
                            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                        `;
                        errorContainer.appendChild(successAlert);

                        // Auto-dismiss after 5 seconds
                        setTimeout(() => {
                            successAlert.remove();
                        }, 5000);

                        // Refresh history to see if new requests are using the right model
                        refreshHistoryTable();
                    } else {
                        alert('Error: ' + data.message);
                    }
                })
                .catch(error => {
                    alert('Error: ' + error);
                });
            });

            // Manual refresh history
            document.getElementById('refreshHistory').addEventListener('click', function() {
                refreshHistoryTable();
            });

            // Auto-refresh history table every 10 seconds
            function refreshHistoryTable() {
                fetch('/api/history')
                .then(response => response.json())
                .then(data => {
                    const historyTable = document.getElementById('historyTable').getElementsByTagName('tbody')[0];
                    historyTable.innerHTML = '';

                    data.history.forEach(req => {
                        const row = historyTable.insertRow();
                        row.className = req.status === 'success' ? 'history-row-success' : 'history-row-error';

                        const timeCell = row.insertCell(0);
                        timeCell.textContent = req.timestamp;

                        const originalCell = row.insertCell(1);
                        const originalSpan = document.createElement('span');
                        originalSpan.className = 'model-name ' +
                            (req.original_model.toLowerCase().includes('claude') ? 'model-claude' : 'model-openai');
                        originalSpan.textContent = req.original_model;
                        originalCell.appendChild(originalSpan);

                        const mappedCell = row.insertCell(2);
                        const mappedSpan = document.createElement('span');
                        mappedSpan.className = 'model-name ' +
                            (req.mapped_model.toLowerCase().includes('claude') ? 'model-claude' : 'model-openai');
                        mappedSpan.textContent = req.mapped_model;
                        mappedCell.appendChild(mappedSpan);

                        const messagesCell = row.insertCell(3);
                        messagesCell.textContent = req.num_messages;

                        const statusCell = row.insertCell(4);
                        statusCell.textContent = req.status;
                        statusCell.className = req.status === 'success' ? 'status-success' : 'status-error';

                        // Add the error column
                        const errorCell = row.insertCell(5);
                        if (req.status === 'error' && req.error) {
                            const errorBadge = document.createElement('span');
                            errorBadge.className = 'badge bg-danger';
                            errorBadge.textContent = req.error;
                            errorCell.appendChild(errorBadge);

                            // Make it clickable to show full error
                            errorBadge.style.cursor = 'pointer';
                            errorBadge.addEventListener('click', function() {
                                // Create and show a modal with full error details
                                const errorContainer = document.getElementById('errorContainer');
                                const errorAlert = document.createElement('div');
                                errorAlert.className = 'alert alert-danger alert-dismissible fade show';
                                errorAlert.innerHTML = `
                                    <h5>Error Details:</h5>
                                    <pre>${req.error}</pre>
                                    <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                                `;
                                errorContainer.appendChild(errorAlert);

                                // Scroll to the top to see the error
                                window.scrollTo(0, 0);
                            });
                        }
                    });
                })
                .catch(error => {
                    console.error('Error refreshing history:', error);
                });
            }

            // Set up auto-refresh
            let autoRefreshInterval = setInterval(refreshHistoryTable, 10000);

            // Initial load of the history table
            refreshHistoryTable();

            // Auto-retry logic for common API errors
            window.addEventListener('error', function(event) {
                // Check if the error is from an API call
                if (event.message && event.message.includes('API')) {
                    // For specific errors like "Anthropic API is overloaded", we'll auto-retry
                    if (event.message.includes('overloaded') ||
                        event.message.includes('rate limit') ||
                        event.message.includes('timeout')) {

                        console.log('API error detected. Will auto-retry in 30 seconds...');
                        // Show a user-friendly message
                        const errorMessage = document.createElement('div');
                        errorMessage.className = 'alert alert-warning alert-dismissible fade show';
                        errorMessage.innerHTML = `
                            <strong>API Error:</strong> ${event.message}
                            <br>Will automatically retry in 30 seconds...
                            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                        `;

                        // Add error to the dedicated error container
                        const errorContainer = document.getElementById('errorContainer');
                        errorContainer.appendChild(errorMessage);

                        // Auto-retry the request after 30 seconds
                        setTimeout(function() {
                            // Remove the error message
                            errorMessage.remove();
                            // Refresh the page to retry
                            window.location.reload();
                        }, 30000);
                    }
                }
            });
        </script>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    # Write the HTML template to the file
    with open("templates/index.html", "w") as f:
        f.write(index_html)

# Define ANSI color codes for terminal output
class Colors:
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"

def log_request_beautifully(method, path, claude_model, openai_model, num_messages, num_tools, status_code):
    """Log requests in a beautiful, twitter-friendly format showing Claude to OpenAI mapping."""
    # This function has been modified to disable terminal logging
    # The web view still has access to these logs

    # Simply return without printing anything to the terminal
    return

    # Format the Claude model name nicely (code kept for reference but not executed)
    claude_display = f"{Colors.CYAN}{claude_model}{Colors.RESET}"

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print("Run with: uvicorn server:app --reload --host 0.0.0.0 --port 8082")
        sys.exit(0)

    # Configure uvicorn to run with minimal logs
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8082,
        log_level="critical",  # Only show critical errors
        access_log=False       # Disable access logs completely
    )
