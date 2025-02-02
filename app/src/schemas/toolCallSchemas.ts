import { z } from "zod";
import zodToJsonSchema from "zod-to-json-schema";

import { assertUnreachable } from "@phoenix/typeUtils";

import { JSONLiteral, jsonLiteralSchema } from "./jsonLiteralSchema";

/**
 * The schema for an OpenAI tool call, this is what a message that calls a tool looks like
 *
 * Note: The nested passThrough's are used to allow for extra keys in JSON schema, however, they do not actually
 * allow for extra keys when the zod schema is used for parsing. This is to allow more flexibility for users
 * to define their own tool calls according
 */
export const openAIToolCallSchema = z.object({
  id: z.string().describe("The ID of the tool call"),
  function: z
    .object({
      name: z.string().describe("The name of the function"),
      // TODO(Parker): The arguments here should not actually be a string, however this is a relic from the current way we stream tool calls where the chunks will come in as strings of partial json objects fix this here: https://github.com/Arize-ai/phoenix/issues/5269
      arguments: z
        .record(z.unknown())
        .describe("The arguments for the function"),
    })
    .describe("The function that is being called")
    .passthrough(),
});

/**
 * The type of an OpenAI tool call
 *
 * @example
 * ```typescript
 *  {
 *   id: "1",
 *   function: {
 *     name: "getCurrentWeather",
 *     arguments: { "city": "San Francisco" }
 *   }
 * }
 * ```
 */
export type OpenAIToolCall = z.infer<typeof openAIToolCallSchema>;

/**
 * The zod schema for multiple OpenAI Tool Calls
 */
export const openAIToolCallsSchema = z.array(openAIToolCallSchema);

/**
 * The JSON schema for multiple OpenAI tool calls
 */
export const openAIToolCallsJSONSchema = zodToJsonSchema(
  openAIToolCallsSchema,
  {
    removeAdditionalStrategy: "passthrough",
  }
);

/**
 * The schema for an Anthropic tool call, this is what a message that calls a tool looks like
 */
export const anthropicToolCallSchema = z
  .object({
    id: z.string().describe("The ID of the tool call"),
    type: z.literal("tool_use"),
    name: z.string().describe("The name of the tool"),
    input: z.record(z.unknown()).describe("The input for the tool"),
  })
  .passthrough();

/**
 * The type of an Anthropic tool call
 */
export type AnthropicToolCall = z.infer<typeof anthropicToolCallSchema>;

/**
 * The zod schema for multiple Anthropic tool calls
 */
export const anthropicToolCallsSchema = z.array(anthropicToolCallSchema);

/**
 * The JSON schema for multiple Anthropic tool calls
 */
export const anthropicToolCallsJSONSchema = zodToJsonSchema(
  anthropicToolCallsSchema,
  {
    removeAdditionalStrategy: "passthrough",
  }
);

/**
 * --------------------------------
 * Conversion Schemas
 * --------------------------------
 */

/**
 * Parse incoming object as an Anthropic tool call and immediately convert to OpenAI format
 */
export const anthropicToolCallToOpenAI = anthropicToolCallSchema.transform(
  (anthropic): OpenAIToolCall => ({
    id: anthropic.id,
    function: {
      name: anthropic.name,
      arguments: anthropic.input,
    },
  })
);

/**
 * Parse incoming object as an OpenAI tool call and immediately convert to Anthropic format
 */
export const openAIToolCallToAnthropic = openAIToolCallSchema.transform(
  (openai): AnthropicToolCall => ({
    id: openai.id,
    type: "tool_use",
    name: openai.function.name,
    // TODO(parker): see comment in openai schema above, fix this here https://github.com/Arize-ai/phoenix/issues/5269
    input:
      typeof openai.function.arguments === "string"
        ? { [openai.function.arguments]: openai.function.arguments }
        : (openai.function.arguments ?? {}),
  })
);

/**
 * --------------------------------
 * Conversion Helpers
 * --------------------------------
 */

/**
 * Union of all tool call formats
 *
 * This is useful for functions that need to accept any tool call format
 */
export const llmProviderToolCallSchema = z.union([
  openAIToolCallSchema,
  anthropicToolCallSchema,
  jsonLiteralSchema,
]);

export type LlmProviderToolCall = z.infer<typeof llmProviderToolCallSchema>;

/**
 * A union of all the lists of tool call formats
 *
 * This is useful for parsing all of the tool calls in a message
 */
export const llmProviderToolCallsSchema = z.array(llmProviderToolCallSchema);

export type LlmProviderToolCalls = z.infer<typeof llmProviderToolCallsSchema>;

type ToolCallWithProvider =
  | {
      provider: Extract<ModelProvider, "OPENAI" | "AZURE_OPENAI">;
      validatedToolCall: OpenAIToolCall;
    }
  | {
      provider: Extract<ModelProvider, "ANTHROPIC">;
      validatedToolCall: AnthropicToolCall;
    }
  | { provider: "UNKNOWN"; validatedToolCall: null };

/**
 * Detect the provider of a tool call object
 */
export const detectToolCallProvider = (
  toolCall: unknown
): ToolCallWithProvider => {
  const { success: openaiSuccess, data: openaiData } =
    openAIToolCallSchema.safeParse(toolCall);
  if (openaiSuccess) {
    // we cannot disambiguate between azure openai and openai here
    return { provider: "OPENAI", validatedToolCall: openaiData };
  }
  const { success: anthropicSuccess, data: anthropicData } =
    anthropicToolCallSchema.safeParse(toolCall);
  if (anthropicSuccess) {
    return { provider: "ANTHROPIC", validatedToolCall: anthropicData };
  }
  return { provider: "UNKNOWN", validatedToolCall: null };
};

type ProviderToToolCallMap = {
  OPENAI: OpenAIToolCall;
  AZURE_OPENAI: OpenAIToolCall;
  ANTHROPIC: AnthropicToolCall;
  // Use generic JSON type for unknown tool formats / new providers
  GEMINI: JSONLiteral;
};

/**
 * Converts a tool call to the OpenAI format if possible
 * @param toolCall a tool call from an unknown LlmProvider
 * @returns the tool call parsed to the OpenAI format
 */
export const toOpenAIToolCall = (
  toolCall: LlmProviderToolCall
): OpenAIToolCall | null => {
  const { provider, validatedToolCall } = detectToolCallProvider(toolCall);
  switch (provider) {
    case "AZURE_OPENAI":
    case "OPENAI":
      return validatedToolCall;
    case "ANTHROPIC":
      return anthropicToolCallToOpenAI.parse(validatedToolCall);
    case "UNKNOWN":
      return null;
    default:
      assertUnreachable(provider);
  }
};

/**
 * Converts a tool call to a target provider format
 * @param toolCall the tool call to convert
 * @param targetProvider the provider to convert the tool call to
 * @returns the tool call in the target provider format
 */
export const fromOpenAIToolCall = <T extends ModelProvider>({
  toolCall,
  targetProvider,
}: {
  toolCall: OpenAIToolCall;
  targetProvider: T;
}): ProviderToToolCallMap[T] => {
  switch (targetProvider) {
    case "AZURE_OPENAI":
    case "OPENAI":
      return toolCall as ProviderToToolCallMap[T];
    case "ANTHROPIC":
      return openAIToolCallToAnthropic.parse(
        toolCall
      ) as ProviderToToolCallMap[T];
    case "GEMINI":
      return toolCall as ProviderToToolCallMap[T];
    default:
      assertUnreachable(targetProvider);
  }
};

/**
 * Creates an empty OpenAI tool call with fields but no values filled in
 */
export function createOpenAIToolCall(): OpenAIToolCall {
  return {
    id: "",
    function: {
      name: "",
      arguments: {},
    },
  };
}

/**
 * Creates an empty Anthropic tool call with fields but no values filled in
 */
export function createAnthropicToolCall(): AnthropicToolCall {
  return {
    id: "",
    type: "tool_use",
    name: "",
    input: {},
  };
}
