const form=document.querySelector('#loginForm');
const errorBox=document.querySelector('#loginError');
form.addEventListener('submit',async event=>{
  event.preventDefault(); const button=form.querySelector('button'); button.disabled=true; errorBox.hidden=true;
  try{
    const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:form.username.value,password:form.password.value})});
    const data=await response.json().catch(()=>({}));
    if(!response.ok) throw new Error(data.error||`Ошибка ${response.status}`);
    location.replace('/');
  }catch(error){ errorBox.textContent=error.message; errorBox.hidden=false; form.password.select(); }
  finally{ button.disabled=false; }
});
