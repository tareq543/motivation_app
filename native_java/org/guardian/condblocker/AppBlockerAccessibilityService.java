package com.tareq.motivator;

import android.accessibilityservice.AccessibilityService;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public class AppBlockerAccessibilityService extends AccessibilityService {

    private String lastLockState = "LOCKED";
    private boolean onboardingComplete = false;
    private long lastStateReadMs = 0;
    private final Set<String> blockedPackages = new HashSet<>();

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null || event.getPackageName() == null) {
            return;
        }

        String pkg = event.getPackageName().toString();
        String myPkg = getPackageName();

        // تجاهل إشعارات النظام العابرة واحتفظ بالتطبيق الحقيقي المفتوح
        if (!"com.android.systemui".equals(pkg)) {
            writeCurrentForegroundPackage(pkg);
        }

        readSharedStateIfNeeded();

        // لا تفرض الحماية الصارمة قبل إكمال الإعداد الأولي أو أثناء نافذة الـ 30 دقيقة
        if (!onboardingComplete || "UNLOCKED_WINDOW".equals(lastLockState)) {
            return;
        }

        // منع فعلي لا يعتمد على نافذة الـ Overlay: أي تطبيق محظور لم يكتمل شرطه
        // يُخرج المستخدم منه فوراً إلى الشاشة الرئيسية.
        if (!myPkg.equals(pkg) && blockedPackages.contains(pkg)) {
            performGlobalAction(GLOBAL_ACTION_HOME);
            return;
        }

        // حماية ضد إيقاف التطبيق إجبارياً أو سحب Device Admin / Accessibility
        if ("com.android.settings".equals(pkg)
                || pkg.contains("packageinstaller")
                || pkg.contains("permissioncontroller")) {

            String cls = event.getClassName() != null ? event.getClassName().toString().toLowerCase() : "";
            AccessibilityNodeInfo root = getRootInActiveWindow();

            boolean isDangerScreen = cls.contains("installedappdetails")
                    || cls.contains("appinfo")
                    || cls.contains("deviceadminadd")
                    || cls.contains("deviceadminsettings")
                    || cls.contains("accessibility")
                    || cls.contains("uninstall");

            if (!isDangerScreen && root != null) {
                isDangerScreen = containsDangerousKeywords(root, myPkg);
            }

            if (isDangerScreen) {
                performGlobalAction(GLOBAL_ACTION_BACK);
                performGlobalAction(GLOBAL_ACTION_HOME);
            }
        }
    }

    private boolean containsDangerousKeywords(AccessibilityNodeInfo root, String myPkg) {
        String[] appIdentifiers = new String[]{
                "Motivator", "الحظر المشروط", myPkg
        };
        boolean mentionsOurApp = false;
        for (String id : appIdentifiers) {
            List<AccessibilityNodeInfo> found = root.findAccessibilityNodeInfosByText(id);
            if (found != null && !found.isEmpty()) {
                mentionsOurApp = true;
                break;
            }
        }
        if (!mentionsOurApp) {
            return false;
        }

        String[] dangerTokens = new String[]{
                "Force stop", "Uninstall", "Deactivate", "إيقاف إجباري", "فرض الإيقاف",
                "إلغاء التثبيت", "إلغاء التنشيط", "تعطيل", "App info", "معلومات التطبيق",
                "Device admin", "مشرف الجهاز", "Accessibility", "إمكانية الوصول"
        };
        for (String token : dangerTokens) {
            List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByText(token);
            if (nodes != null && !nodes.isEmpty()) {
                return true;
            }
        }
        return false;
    }

    private void writeCurrentForegroundPackage(String pkg) {
        try {
            File f = new File(getFilesDir(), "foreground_pkg.txt");
            try (FileOutputStream fos = new FileOutputStream(f, false)) {
                fos.write(pkg.getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
        }
    }

    private void readSharedStateIfNeeded() {
        long now = System.currentTimeMillis();
        if (now - lastStateReadMs < 1000) {
            return;
        }
        lastStateReadMs = now;
        try {
            File f = new File(getFilesDir(), "blocker_state.json");
            if (!f.exists()) {
                return;
            }
            byte[] buf = new byte[(int) f.length()];
            try (FileInputStream fis = new FileInputStream(f)) {
                int read = fis.read(buf);
                if (read > 0) {
                    JSONObject obj = new JSONObject(new String(buf, 0, read, StandardCharsets.UTF_8));
                    lastLockState = obj.optString("lock_state", "LOCKED");
                    onboardingComplete = obj.optBoolean("onboarding_complete", false);
                    blockedPackages.clear();
                    JSONArray arr = obj.optJSONArray("blocked_packages");
                    if (arr != null) {
                        for (int i = 0; i < arr.length(); i++) {
                            String bp = arr.optString(i, "");
                            if (!bp.isEmpty()) {
                                blockedPackages.add(bp);
                            }
                        }
                    }
                }
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onInterrupt() {
    }
}
