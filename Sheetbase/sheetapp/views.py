from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import redirect, render

from .forms import SignUpForm


def home(request):
    return render(request, "sheetapp/home.html")


def signup(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, f"Welcome, {user}! Your account has been created.")
        return redirect("home")
    return render(request, "registration/signup.html", {"form": form})
