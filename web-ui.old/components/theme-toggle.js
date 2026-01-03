/**
 * Theme Toggle Component
 * 
 * Provides user-selectable light/dark theme that persists via localStorage.
 * Also exports Plotly configuration helpers for consistent chart styling.
 */

// ============================================================================
// Theme Management
// ============================================================================

const THEME_KEY = 'timestd-theme';
const CHART_THEME_KEY = 'timestd-chart-theme';

/**
 * Available themes
 */
const THEMES = {
  dark: 'dark',
  light: 'light'
};

const CHART_THEMES = {
  dark: 'dark',
  light: 'light',
  auto: 'auto'  // Match page theme
};

/**
 * Get current theme from localStorage or default to dark
 */
function getTheme() {
  return localStorage.getItem(THEME_KEY) || THEMES.dark;
}

/**
 * Get current chart theme from localStorage or default to auto
 */
function getChartTheme() {
  return localStorage.getItem(CHART_THEME_KEY) || CHART_THEMES.auto;
}

/**
 * Set theme and apply to document
 */
function setTheme(theme) {
  localStorage.setItem(THEME_KEY, theme);
  applyTheme(theme);
  
  // Update charts if chart theme is 'auto'
  if (getChartTheme() === 'auto') {
    window.dispatchEvent(new CustomEvent('themeChanged', { detail: { theme } }));
  }
}

/**
 * Set chart theme
 */
function setChartTheme(chartTheme) {
  localStorage.setItem(CHART_THEME_KEY, chartTheme);
  window.dispatchEvent(new CustomEvent('chartThemeChanged', { detail: { chartTheme } }));
}

/**
 * Apply theme class to body
 */
function applyTheme(theme) {
  document.body.classList.remove('light-theme', 'dark-theme');
  if (theme === THEMES.light) {
    document.body.classList.add('light-theme');
  } else {
    document.body.classList.add('dark-theme');
  }
  
  // Update theme toggle button if present
  const toggle = document.getElementById('theme-toggle-btn');
  if (toggle) {
    toggle.textContent = theme === THEMES.dark ? '☀️' : '🌙';
    toggle.title = theme === THEMES.dark ? 'Switch to Light Theme' : 'Switch to Dark Theme';
  }
}

/**
 * Toggle between light and dark theme
 */
function toggleTheme() {
  const current = getTheme();
  const next = current === THEMES.dark ? THEMES.light : THEMES.dark;
  setTheme(next);
}

/**
 * Initialize theme on page load
 */
function initTheme() {
  applyTheme(getTheme());
}

// ============================================================================
// Plotly Configuration Helpers
// ============================================================================

/**
 * Get the effective chart theme (resolves 'auto' to actual theme)
 */
function getEffectiveChartTheme() {
  const chartTheme = getChartTheme();
  if (chartTheme === 'auto') {
    return getTheme();
  }
  return chartTheme;
}

/**
 * Standard Plotly modebar configuration
 * - Shows on hover only (doesn't block title)
 * - Removes Plotly logo
 * - Minimal useful buttons
 */
var PLOTLY_CONFIG = {
  displayModeBar: 'hover',
  displaylogo: false,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
  modeBarButtonsToAdd: [],
  responsive: true,
  scrollZoom: false
};

/**
 * Get Plotly layout colors based on current chart theme
 */
function getPlotlyColors() {
  const theme = getEffectiveChartTheme();
  
  if (theme === 'light') {
    return {
      paper_bgcolor: 'rgba(255, 255, 255, 0.95)',
      plot_bgcolor: 'rgba(248, 250, 252, 0.9)',
      font_color: '#1e293b',
      grid_color: 'rgba(100, 116, 139, 0.2)',
      axis_color: '#64748b',
      title_color: '#0f172a',
      legend_bgcolor: 'rgba(255, 255, 255, 0.9)'
    };
  } else {
    return {
      paper_bgcolor: 'rgba(0, 0, 0, 0)',
      plot_bgcolor: 'rgba(15, 23, 42, 0.6)',
      font_color: '#e0e0e0',
      grid_color: 'rgba(139, 92, 246, 0.15)',
      axis_color: '#94a3b8',
      title_color: '#ffffff',
      legend_bgcolor: 'rgba(30, 41, 59, 0.9)'
    };
  }
}

/**
 * Get standard Plotly layout with theme-appropriate colors
 * @param {Object} overrides - Layout properties to override defaults
 */
function getPlotlyLayout(overrides = {}) {
  const colors = getPlotlyColors();
  
  const baseLayout = {
    paper_bgcolor: colors.paper_bgcolor,
    plot_bgcolor: colors.plot_bgcolor,
    font: {
      color: colors.font_color,
      family: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
    },
    title: {
      font: {
        color: colors.title_color,
        size: 14
      },
      x: 0.01,  // Left-align title to avoid modebar overlap
      xanchor: 'left'
    },
    xaxis: {
      gridcolor: colors.grid_color,
      linecolor: colors.axis_color,
      tickfont: { color: colors.axis_color },
      titlefont: { color: colors.font_color }
    },
    yaxis: {
      gridcolor: colors.grid_color,
      linecolor: colors.axis_color,
      tickfont: { color: colors.axis_color },
      titlefont: { color: colors.font_color }
    },
    legend: {
      bgcolor: colors.legend_bgcolor,
      bordercolor: 'rgba(139, 92, 246, 0.3)',
      borderwidth: 1,
      font: { color: colors.font_color }
    },
    margin: { l: 60, r: 40, t: 50, b: 50 },
    hoverlabel: {
      bgcolor: colors.legend_bgcolor,
      bordercolor: 'rgba(139, 92, 246, 0.5)',
      font: { color: colors.font_color }
    }
  };
  
  // Deep merge overrides
  return deepMerge(baseLayout, overrides);
}

/**
 * Deep merge two objects
 */
function deepMerge(target, source) {
  const output = Object.assign({}, target);
  if (isObject(target) && isObject(source)) {
    Object.keys(source).forEach(key => {
      if (isObject(source[key])) {
        if (!(key in target)) {
          Object.assign(output, { [key]: source[key] });
        } else {
          output[key] = deepMerge(target[key], source[key]);
        }
      } else {
        Object.assign(output, { [key]: source[key] });
      }
    });
  }
  return output;
}

function isObject(item) {
  return (item && typeof item === 'object' && !Array.isArray(item));
}

/**
 * Station colors (consistent across themes)
 */
const STATION_COLORS = {
  wwv: '#3498db',        // Blue
  wwvh: '#e67e22',       // Orange
  groundTruth: '#10b981', // Green
  error: '#ef4444',      // Red
  warning: '#f59e0b',    // Amber
  success: '#22c55e',    // Green
  accent: '#8b5cf6',     // Purple
  solar: {
    wwv: '#e74c3c',      // Red for WWV path
    wwvh: '#9b59b6'      // Purple for WWVH path
  }
};

// ============================================================================
// Theme Toggle UI Component
// ============================================================================

/**
 * Create and inject theme toggle into navigation
 */
function createThemeToggle() {
  // Find nav-status container (in navigation.js)
  const navStatus = document.querySelector('.nav-status');
  if (!navStatus) {
    // Create standalone toggle if no nav
    createStandaloneToggle();
    return;
  }
  
  // Add theme toggle button before status indicator
  const toggleBtn = document.createElement('button');
  toggleBtn.id = 'theme-toggle-btn';
  toggleBtn.className = 'theme-toggle';
  toggleBtn.title = getTheme() === 'dark' ? 'Switch to Light Theme' : 'Switch to Dark Theme';
  toggleBtn.textContent = getTheme() === 'dark' ? '☀️' : '🌙';
  toggleBtn.onclick = toggleTheme;
  
  navStatus.insertBefore(toggleBtn, navStatus.firstChild);
  
  // Add styles
  addThemeToggleStyles();
}

/**
 * Create standalone toggle (bottom-right corner)
 */
function createStandaloneToggle() {
  const toggle = document.createElement('button');
  toggle.id = 'theme-toggle-btn';
  toggle.className = 'theme-toggle floating';
  toggle.title = getTheme() === 'dark' ? 'Switch to Light Theme' : 'Switch to Dark Theme';
  toggle.textContent = getTheme() === 'dark' ? '☀️' : '🌙';
  toggle.onclick = toggleTheme;
  
  document.body.appendChild(toggle);
  addThemeToggleStyles();
}

/**
 * Add CSS for theme toggle button
 */
function addThemeToggleStyles() {
  if (document.getElementById('theme-toggle-styles')) return;
  
  const style = document.createElement('style');
  style.id = 'theme-toggle-styles';
  style.textContent = `
    .theme-toggle {
      background: rgba(139, 92, 246, 0.2);
      border: 1px solid rgba(139, 92, 246, 0.4);
      color: var(--text-primary);
      padding: 6px 10px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 16px;
      transition: all 0.2s ease;
      margin-right: 12px;
    }
    
    .theme-toggle:hover {
      background: rgba(139, 92, 246, 0.4);
      transform: scale(1.05);
    }
    
    .theme-toggle.floating {
      position: fixed;
      bottom: 20px;
      right: 20px;
      z-index: 1000;
      padding: 12px 16px;
      font-size: 20px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    }
    
    /* Light theme button adjustments */
    .light-theme .theme-toggle {
      background: rgba(100, 116, 139, 0.15);
      border-color: rgba(100, 116, 139, 0.3);
    }
    
    .light-theme .theme-toggle:hover {
      background: rgba(100, 116, 139, 0.25);
    }
  `;
  
  document.head.appendChild(style);
}

// ============================================================================
// Initialize on DOM Ready
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  
  // Create toggle after a short delay to ensure navigation is loaded
  setTimeout(createThemeToggle, 100);
});

// Export for use in other modules
window.TIMESTDTheme = {
  getTheme,
  setTheme,
  toggleTheme,
  getChartTheme,
  setChartTheme,
  getEffectiveChartTheme,
  THEMES,
  CHART_THEMES,
  PLOTLY_CONFIG,
  getPlotlyLayout,
  getPlotlyColors,
  STATION_COLORS
};
