As an experienced and professional reverse engineer, you possess the ability to evaluate decompiled code in an objective and impartial manner.
I will provide you with the source code that needs evaluation, followed by the decompiled version of that code.
Your task is to assess the decompiled code's quality for **Function Signature Recovery** only.

You will have three scoring options (2, 1, 0) where 2 means perfect with no issues, 1 means almost perfect with minor issues, and 0 means significant issues or basically the same as raw decompiler output.

## **Evaluation Criterion: Function Signature Recovery**

- Example:
    - Source code:

        ```c
        int process(const char *input, size_t len, int flags);
        ```

    - Decompiled code:

        ```c
        __int64 sub_1234(__int64 a1, __int64 a2, __int64 a3);
        ```

    - **Explanation**: Incorrect parameter counts, types, or return types make it difficult to understand how to call the function and what it does.

- Evaluation Choices:
    - 2: The decompiled code correctly recovers function signatures (parameters, types, return type).
    - 1: The decompiled code has minor signature issues but parameter count is correct.
    - 0: The decompiled code has incorrect function signatures (wrong parameter count or types).

**Think step by step** and evaluate the decompiled code compared to the source code for this aspect.

**First**, provide your reasoning about function signature recovery issues present (or absent) in the decompiled code.

**Finally**, output a JSON object with your score and reasoning:

```json
{
    "function_signature_recovery": {"score": <0, 1, or 2>, "reasoning": "<brief explanation>"}
}
```
