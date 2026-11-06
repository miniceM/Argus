/* Issue #249 的真实 TCP/Java 认证夹具，不是 AgentScope，不调用模型。
 * 仅绑定 loopback；所有凭据均为固定的无价值测试值。不可用于生产部署。 */
import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpExchange;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.UUID;
import java.util.concurrent.Executors;

public class AuthAgent {
    private static volatile String current = "fixture-first-token";
    private static volatile int invocations = 0;
    private static void respond(HttpExchange exchange, int code, String body) throws Exception {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.getResponseHeaders().set("X-Demo-Traceparent-Received", Boolean.toString(exchange.getRequestHeaders().containsKey("traceparent")));
        exchange.sendResponseHeaders(code, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }
    public static void main(String[] args) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try {
                String path = exchange.getRequestURI().getPath();
                if (path.equals("/rotate")) { current = "fixture-second-token"; respond(exchange, 200, "{}"); return; }
                if (path.equals("/reset")) { current = "fixture-first-token"; invocations = 0; respond(exchange, 200, "{}"); return; }
                if (path.equals("/stats")) { respond(exchange, 200, "{\"calls\":" + invocations + "}"); return; }
                invocations++;
                var headers = exchange.getRequestHeaders();
                String auth = headers.getFirst("Authorization");
                boolean authenticated = switch (path) {
                    case "/none" -> auth == null;
                    case "/key" -> current.equals(headers.getFirst("X-API-Key"));
                    case "/raw" -> ("Token " + current).equals(auth);
                    case "/basic" -> ("Basic " + Base64.getEncoder().encodeToString("fixture user:fixture password".getBytes(StandardCharsets.UTF_8))).equals(auth);
                    default -> ("Bearer " + current).equals(auth);
                };
                if (!authenticated) { respond(exchange, 401, "{\"error\":\"unauthorized\"}"); return; }
                if (!"evaluation".equals(headers.getFirst("X-Tenant")) || !"argus-acceptance".equals(headers.getFirst("X-App-Id")) || !"fixture-extra-secret".equals(headers.getFirst("X-App-Secret"))) {
                    respond(exchange, 403, "{\"error\":\"required headers missing\"}"); return;
                }
                if (path.equals("/forbidden")) { respond(exchange, 403, "{\"error\":\"RUN denied (fixture)\"}"); return; }
                if (path.equals("/missing")) { respond(exchange, 404, "{\"error\":\"not visible (fixture)\"}"); return; }
                if (path.equals("/echo")) { respond(exchange, 200, "{\"reply\":\"" + current + "\"}"); return; }
                if (path.equals("/redirect")) {
                    exchange.getResponseHeaders().set("Location", "/bearer"); respond(exchange, 307, "{}"); return;
                }
                String request = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                if (!request.contains("\"message\"") || request.contains("\"sessionKey\"")) { respond(exchange, 422, "{\"error\":\"message required, session omitted per item\"}"); return; }
                if (path.equals("/invalid-output")) { respond(exchange, 200, "{\"reply\":null}"); return; }
                respond(exchange, 200, "{\"reply\":\"java business answer\",\"intent\":\"refund\",\"sessionKey\":\"" + UUID.randomUUID() + "\"}");
            } catch (Exception failure) {
                try { respond(exchange, 500, "{\"error\":\"fixture failure\"}"); } catch (Exception ignored) { exchange.close(); }
            }
        });
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
        System.out.println(server.getAddress().getPort());
        System.out.flush();
    }
}
