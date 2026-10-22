import { describe, expect, it } from "vitest";
import { extractCaseTitle } from "../tabs/CasesTraceTab";

describe("extractCaseTitle", () => {
  it("extracts user message content from messages array input", () => {
    const dataset = {
      items: [
        {
          id: "item-1",
          input: {
            messages: [
              { role: "system", content: "You are a banking assistant." },
              { role: "user", content: "请帮我查询昨天账户中的异常交易" },
            ],
          },
        },
      ],
    };
    const res = extractCaseTitle("item-1", dataset);
    expect(res).toEqual({
      title: "请帮我查询昨天账户中的异常交易",
      isPrompt: true,
    });
  });

  it("extracts plain string input", () => {
    const dataset = {
      items: [
        {
          id: "item-2",
          input: "转账 500 元给张三",
        },
      ],
    };
    const res = extractCaseTitle("item-2", dataset);
    expect(res).toEqual({
      title: "转账 500 元给张三",
      isPrompt: true,
    });
  });

  it("extracts prompt or query fields", () => {
    const dataset = {
      items: [
        {
          id: "item-3",
          input: { query: "挂失银行卡" },
        },
      ],
    };
    const res = extractCaseTitle("item-3", dataset);
    expect(res).toEqual({
      title: "挂失银行卡",
      isPrompt: true,
    });
  });

  it("gracefully falls back to dataset_item_id when input is missing or empty", () => {
    const dataset = {
      items: [
        {
          id: "item-4",
          input: {},
        },
      ],
    };
    const res = extractCaseTitle("item-4", dataset);
    expect(res).toEqual({
      title: "item-4",
      isPrompt: false,
    });

    const resMissing = extractCaseTitle("non-existent", dataset);
    expect(resMissing).toEqual({
      title: "non-existent",
      isPrompt: false,
    });
  });
});
