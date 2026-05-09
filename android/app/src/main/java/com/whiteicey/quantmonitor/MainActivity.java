package com.whiteicey.quantmonitor;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.LinearLayout;
import android.graphics.Color;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public class MainActivity extends Activity {
    private WebView webView;
    private LinearLayout loadingView;
    private TextView statusText;
    private static final String SERVER_URL = "http://127.0.0.1:5000";
    private static final int MAX_RETRIES = 30;
    
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        
        // Fullscreen
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().setStatusBarColor(Color.parseColor("#080c14"));
        getWindow().setNavigationBarColor(Color.parseColor("#080c14"));
        
        // Create layout programmatically
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#080c14"));
        
        // Loading view
        loadingView = new LinearLayout(this);
        loadingView.setOrientation(LinearLayout.VERTICAL);
        loadingView.setGravity(android.view.Gravity.CENTER);
        loadingView.setBackgroundColor(Color.parseColor("#080c14"));
        
        statusText = new TextView(this);
        statusText.setText("正在启动服务...");
        statusText.setTextColor(Color.parseColor("#d4a574"));
        statusText.setTextSize(16);
        statusText.setGravity(android.view.Gravity.CENTER);
        statusText.setPadding(0, 20, 0, 0);
        
        ProgressBar progress = new ProgressBar(this);
        progress.setIndeterminate(true);
        
        loadingView.addView(progress, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        loadingView.addView(statusText, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        
        // WebView
        webView = new WebView(this);
        webView.setVisibility(View.GONE);
        webView.setBackgroundColor(Color.parseColor("#080c14"));
        
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        
        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient());
        
        root.addView(loadingView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.MATCH_PARENT));
        root.addView(webView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.MATCH_PARENT));
        
        setContentView(root);
        
        // Start Python Flask server in background thread
        startFlaskServer();
    }
    
    private void startFlaskServer() {
        // Initialize Python
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        
        new Thread(() -> {
            try {
                Python py = Python.getInstance();
                // Run the Flask app
                py.getModule("app_mobile").callAttr("start_server");
            } catch (Exception e) {
                runOnUiThread(() -> statusText.setText("启动失败: " + e.getMessage()));
            }
        }).start();
        
        // Poll until server is ready
        pollServer(0);
    }
    
    private void pollServer(int attempt) {
        new Handler(Looper.getMainLooper()).postDelayed(() -> {
            try {
                java.net.URL url = new java.net.URL(SERVER_URL);
                java.net.HttpURLConnection conn = (java.net.HttpURLConnection) url.openConnection();
                conn.setConnectTimeout(1000);
                conn.setReadTimeout(1000);
                int code = conn.getResponseCode();
                conn.disconnect();
                
                if (code == 200) {
                    // Server is ready
                    loadingView.setVisibility(View.GONE);
                    webView.setVisibility(View.VISIBLE);
                    webView.loadUrl(SERVER_URL);
                    return;
                }
            } catch (Exception ignored) {}
            
            if (attempt < MAX_RETRIES) {
                statusText.setText("正在启动服务... (" + (attempt + 1) + ")");
                pollServer(attempt + 1);
            } else {
                statusText.setText("服务启动超时，请重启应用");
            }
        }, 1000);
    }
    
    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
