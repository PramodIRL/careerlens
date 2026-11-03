import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { saveAiSettings } from "@/lib/ai-settings";

import AssistantPanel from "./assistant-panel";
import Markdown from "./markdown";

const fetchMock = vi.fn();

// An in-memory Storage: this jsdom/Node pairing exposes no usable
// window.localStorage, which ai-settings tolerates but a test that saves
// a key needs.
function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (name) => data.get(name) ?? null,
    key: (index) => [...data.keys()][index] ?? null,
    removeItem: (name) => void data.delete(name),
    setItem: (name, value) => void data.set(name, String(value)),
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  Object.defineProperty(window, "localStorage", {
    value: memoryStorage(),
    configurable: true,
  });
});

describe("Markdown", () => {
  it("renders HTML in a reply as text, never as markup", () => {
    const { container } = render(
      <Markdown
        source={'<img src=x onerror="alert(1)"> **bold** <script>x</script>'}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(screen.getByText("bold").tagName).toBe("STRONG");
  });

  it("keeps http links and drops javascript: ones", () => {
    render(
      <Markdown source="[ok](https://example.com) and [bad](javascript:alert(1))" />,
    );
    expect(screen.getByRole("link", { name: "ok" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
    expect(screen.getByText(/bad/)).toBeInTheDocument();
  });

  it("renders lists", () => {
    render(<Markdown source={"- one\n- two\n\n1. first"} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });
});

describe("AssistantPanel", () => {
  it("shows the add-your-key state when no key is stored", () => {
    render(<AssistantPanel accessToken="tok" />);
    expect(
      screen.getByRole("link", { name: "Add your key in Settings" }),
    ).toHaveAttribute("href", "#settings");
  });

  it("sends the stored key and model as headers and shows tool chips", async () => {
    saveAiSettings("sk-or-v1-browser", "openai/gpt-6-luna");
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          reply: "You are closest to **Acme**.",
          tools_used: ["list_saved_jobs"],
          model: "openai/gpt-6-luna",
        }),
        { status: 200 },
      ),
    );

    render(<AssistantPanel accessToken="tok" />);
    fireEvent.click(
      screen.getByRole("button", { name: "Which job am I closest to?" }),
    );

    await waitFor(() => expect(screen.getByText("Acme")).toBeInTheDocument());
    expect(screen.getByText("Saved jobs")).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/ai\/chat$/);
    expect(init.headers["X-OpenRouter-Key"]).toBe("sk-or-v1-browser");
    expect(init.headers["X-OpenRouter-Model"]).toBe("openai/gpt-6-luna");
    expect(JSON.parse(init.body)).toEqual({
      messages: [{ role: "user", content: "Which job am I closest to?" }],
    });
  });

  it("shows the API's missing-key message with a Settings link", async () => {
    saveAiSettings("sk-or-v1-browser", "openai/gpt-6-luna");
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "Add your OpenRouter key in Settings" }),
        { status: 400 },
      ),
    );

    render(<AssistantPanel accessToken="tok" />);
    fireEvent.click(
      screen.getByRole("button", { name: "What should I learn this week?" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Add your OpenRouter key in Settings",
    );
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute(
      "href",
      "#settings",
    );
    // The question goes back into the box rather than being lost.
    expect(screen.getByLabelText("Message the career assistant")).toHaveValue(
      "What should I learn this week?",
    );
  });
});
