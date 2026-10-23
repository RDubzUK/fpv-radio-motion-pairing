// Report module failures, including incorrect host MIME associations.
(() => {
  const status=document.getElementById('startup-status');
  const controls=document.querySelectorAll('#new-session,#prepare,.browse-folder');
  for(const control of controls)control.disabled=true;
  const timer=setTimeout(()=>{status.textContent='Startup is taking longer than expected. Update and restart the app, then refresh with Ctrl+F5.';},15000);
  import('./app.js?v=77').then(()=>{clearTimeout(timer);status.hidden=true;for(const control of controls)control.disabled=false;}).catch(error=>{
    clearTimeout(timer);status.classList.add('error');status.textContent=`Flight Matcher could not start. Update and restart, then refresh with Ctrl+F5. ${error.message}`;
  });
})();
