package webmc.demo;

import net.fabricmc.api.ClientModInitializer;

/**
 * The smallest thing that proves a jar this machine compiled, and nothing else,
 * is loaded by Fabric inside Minecraft at play time.
 */
public class HelloMod implements ClientModInitializer {
    @Override
    public void onInitializeClient() {
        System.out.println("WEBMC-DEMO-MOD: loaded from a locally compiled jar, by Fabric, at play time");
        System.out.println("WEBMC-DEMO-MOD: java=" + System.getProperty("java.version")
                + " os=" + System.getProperty("os.name") + " " + System.getProperty("os.arch"));
    }
}
