from django import forms
from .models import Module

class ModuleForm(forms.ModelForm):
    class Meta:
        model = Module
        fields = ["level", "name", "grade_percent", "credits", "completion_percent"]
        widgets = {
            "grade_percent": forms.NumberInput(attrs={"step": "0.1", "min": "0", "max": "100"}),
            "credits": forms.NumberInput(attrs={"step": "1", "min": "0", "max": "100"}),
            "completion_percent": forms.NumberInput(attrs={"step": "0.1", "min": "0", "max": "100"}),
        }
