import java.util.*;
import io.agentscope.harness.agent.gateway.channel.*;
import io.agentscope.dataagent.runtime.config.ChannelConfigEntry;
import com.fasterxml.jackson.databind.ObjectMapper;

/** 直接运行固定官方制品的路由；不启动 Agent、Sandbox 或模型。 */
public class SessionScopeProbe {
  static String key(ChannelConfig config,String conversation,String user) {
    var input=InboundMessage.builder("chatui",Peer.direct(user),List.of())
      .preferredAgentId("data-agent").accountId(conversation).build();
    return new ChannelRouter("data-agent").resolveRoute(config,input).context().canonicalKey();
  }
  public static void main(String[] args) throws Exception {
    var mapper=new ObjectMapper();
    var defaults=new ChannelConfigEntry().toChannelConfig("chatui");
    var isolated=mapper.readValue("{\"dmScope\":\"PER_ACCOUNT_CHANNEL_PEER\"}",ChannelConfigEntry.class).toChannelConfig("chatui");
    String a=key(defaults,"case-a","bob"),b=key(defaults,"case-b","bob");
    String c=key(isolated,"case-a","bob"),d=key(isolated,"case-b","bob");
    if(!a.equals(b)||c.equals(d)||!c.equals(key(isolated,"case-a","bob"))||c.equals(key(isolated,"case-a","alice"))) throw new AssertionError("unexpected session routing");
    System.out.println(mapper.writerWithDefaultPrettyPrinter().writeValueAsString(Map.of(
      "official_sha","0b6743b263ba8e39eb417e35048026f1e0196548",
      "default_scope",defaults.dmScope().toString(),"default_case_a",a,"default_case_b",b,
      "default_collides",a.equals(b),"configured_scope",isolated.dmScope().toString(),
      "configured_case_a",c,"configured_case_b",d,"configured_separates",!c.equals(d),
      "different_user_separates",!c.equals(key(isolated,"case-a","alice")))));
  }
}
