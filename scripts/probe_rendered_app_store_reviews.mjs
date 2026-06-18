import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

function parseArgs(argv) {
  const args = {
    scrolls: 8,
    waitMs: 1000,
    headed: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--headed") {
      args.headed = true;
    } else if (arg.startsWith("--")) {
      const key = arg.slice(2).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
      args[key] = argv[index + 1];
      index += 1;
    }
  }
  args.scrolls = Number(args.scrolls);
  args.waitMs = Number(args.waitMs);
  return args;
}

async function collectReviewSignals(page) {
  return page.evaluate(() => {
    const titleNodes = Array.from(document.querySelectorAll('[id^="review-"][id$="-title"]'));
    const titleIds = titleNodes
      .map((node) => node.id.match(/^review-(\d+)-title$/)?.[1])
      .filter(Boolean)
      .sort();
    const titleSamples = titleNodes.slice(0, 10).map((node) => ({
      id: node.id,
      text: node.textContent?.trim().replace(/\s+/g, " ").slice(0, 160) || "",
    }));
    const bodyText = document.body?.innerText || "";
    return {
      reviewTitleIdCount: new Set(titleIds).size,
      reviewTitleIds: Array.from(new Set(titleIds)),
      reviewTitleSamples: titleSamples,
      bodyTextBytes: new TextEncoder().encode(bodyText).length,
      documentHeight: document.documentElement.scrollHeight,
      viewportHeight: window.innerHeight,
    };
  });
}

function interestingNetworkEntry(response, phase) {
  const url = response.url();
  const lowerUrl = url.toLowerCase();
  if (!lowerUrl.includes("reviews") && !lowerUrl.includes("customerreviews")) {
    return null;
  }
  return {
    url,
    phase,
    status: response.status(),
    isApiRequest: lowerUrl.includes("/api/") || lowerUrl.includes("/rss/"),
    contentType: response.headers()["content-type"] || null,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.url || !args.output) {
    console.error(
      "usage: node scripts/probe_rendered_app_store_reviews.mjs --url <app-store-review-url> --output <report.json> [--scrolls 8] [--wait-ms 1000] [--headed]",
    );
    process.exit(2);
  }

  const browser = await chromium.launch({ headless: !args.headed });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
  });
  const network = [];
  let phase = "initial_navigation";
  page.on("response", (response) => {
    const entry = interestingNetworkEntry(response, phase);
    if (entry) {
      network.push(entry);
    }
  });

  const startedAt = new Date().toISOString();
  await page.goto(args.url, { waitUntil: "networkidle", timeout: 60000 });
  const initial = await collectReviewSignals(page);
  const observations = [{ step: "initial", ...initial }];
  phase = "scrolling";

  for (let index = 1; index <= args.scrolls; index += 1) {
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await page.waitForTimeout(args.waitMs);
    observations.push({ step: `scroll_${index}`, ...(await collectReviewSignals(page)) });
  }

  const final = observations[observations.length - 1];
  const initialIds = new Set(initial.reviewTitleIds);
  const finalIds = new Set(final.reviewTitleIds);
  const newIdsAfterScroll = Array.from(finalIds).filter((id) => !initialIds.has(id)).sort();
  const apiReviewRequests = network.filter((entry) => entry.isApiRequest);
  const scrollReviewRequests = network.filter((entry) => entry.phase === "scrolling");
  const report = {
    generated_at: new Date().toISOString(),
    started_at: startedAt,
    source: "app_store_rendered_html_playwright_probe",
    url: args.url,
    settings: {
      scrolls: args.scrolls,
      wait_ms: args.waitMs,
      headed: args.headed,
    },
    initial,
    final,
    new_review_ids_after_scroll: newIdsAfterScroll,
    review_count_changed_after_scroll: final.reviewTitleIdCount !== initial.reviewTitleIdCount,
    network_review_request_count: network.length,
    network_review_api_request_count: apiReviewRequests.length,
    network_review_request_count_after_initial_load: scrollReviewRequests.length,
    network_review_requests: network,
    diagnostic_conclusion: {
      rendered_html_exposed_more_reviews_after_scroll: newIdsAfterScroll.length > 0,
      rendered_html_triggered_review_requests_after_scroll: scrollReviewRequests.length > 0,
      rendered_html_triggered_review_api_requests: apiReviewRequests.length > 0,
    },
  };

  await browser.close();
  await fs.mkdir(path.dirname(args.output), { recursive: true });
  await fs.writeFile(args.output, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ output: args.output, summary: report.diagnostic_conclusion }, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
