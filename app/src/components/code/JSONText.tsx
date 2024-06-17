import React, { useMemo } from "react";
import { css } from "@emotion/react";

import { isObject } from "@phoenix/typeUtils";

const preCSS = css`
  margin: 0;
  white-space: pre-wrap;
  word-wrap: break-word;
`;
/**
 * Truncates the text if it is too long.
 * @param {string} text The text to truncate
 * @returns {string} The truncated text
 */
function formatText(text: string, maxLength: number) {
  if (text.length > maxLength) {
    return `${text.slice(0, maxLength)}...`;
  }
  return text;
}

export function JSONText({
  json,
  maxLength,
  space = 0,
}: {
  json: unknown;
  maxLength?: number;
  space?: number;
}) {
  const hasMaxLength = typeof maxLength === "number";
  const fullValue = useMemo(
    () => JSON.stringify(json, null, space),
    [json, space]
  );
  if (!isObject(json)) {
    // Just show text and log a warning
    // eslint-disable-next-line no-console
    console.warn("JSONText component received a non-object value", json);
    return <span>{String(json)}</span>;
  }
  const obj = json as Record<string, unknown>;
  // If the object has only one key and the value is a string, show the string
  if (Object.keys(obj).length === 1) {
    const key = Object.keys(obj)[0];
    const singleValue = obj[key];
    if (typeof singleValue === "string") {
      const singleValueStr: string = hasMaxLength
        ? formatText(singleValue, maxLength)
        : singleValue;
      return <span title={fullValue}>{singleValueStr}</span>;
    }
  }
  const textValue = hasMaxLength ? formatText(fullValue, maxLength) : fullValue;
  const Element = hasMaxLength ? "span" : "pre";
  const cssStyles = hasMaxLength ? undefined : preCSS;
  return (
    <Element title={fullValue} css={cssStyles}>
      {textValue}
    </Element>
  );
}
