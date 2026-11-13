package org.guardian.condblocker;

import android.app.admin.DeviceAdminReceiver;
import android.content.Context;
import android.content.Intent;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;

public class AppDeviceAdminReceiver extends DeviceAdminReceiver {

    @Override
    public void onEnabled(Context context, Intent intent) {
        Toast.makeText(context, "تم تفعيل حماية مشرف الجهاز للحظر المشروط", Toast.LENGTH_SHORT).show();
    }

    @Override
    public CharSequence onDisableRequested(Context context, Intent intent) {
        String state = readLockState(context);
        if (!"UNLOCKED_WINDOW".equals(state)) {
            Intent home = new Intent(Intent.ACTION_MAIN);
            home.addCategory(Intent.CATEGORY_HOME);
            home.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(home);
            return "لا يمكن تعطيل الحماية أثناء القفل الصارم. ابدأ عداد الـ 24 ساعة من داخل التطبيق أولاً.";
        }
        return "نافذة التعديل مفتوحة: يمكنك تعطيل مشرف الجهاز الآن.";
    }

    @Override
    public void onDisabled(Context context, Intent intent) {
        Toast.makeText(context, "تم تعطيل صلاحية مشرف الجهاز", Toast.LENGTH_SHORT).show();
    }

    private String readLockState(Context context) {
        try {
            File f = new File(context.getFilesDir(), "blocker_state.json");
            if (!f.exists()) return "LOCKED";
            byte[] buf = new byte[(int) f.length()];
            try (FileInputStream fis = new FileInputStream(f)) {
                int r = fis.read(buf);
                if (r > 0) {
                    JSONObject obj = new JSONObject(new String(buf, 0, r, StandardCharsets.UTF_8));
                    return obj.optString("lock_state", "LOCKED");
                }
            }
        } catch (Exception ignored) {
        }
        return "LOCKED";
    }
}
