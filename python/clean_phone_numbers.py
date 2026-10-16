import csv
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

def clean_phone_number(phone):
    """Clean phone number according to the rules."""
    if not phone or phone == '':
        return phone
    
    # Convert to string
    phone = str(phone)
    
    # Remove all spaces
    phone = phone.replace(' ', '')
    
    # Remove dashes and other non-digit characters except +
    phone = re.sub(r'[^\d+]', '', phone)
    
    # If starts with 9 and doesn't have +, add +
    if phone.startswith('9') and not phone.startswith('+'):
        phone = '+' + phone
    
    # If starts with 0, add +90
    if phone.startswith('0'):
        phone = '+90' + phone[1:]
    
    # If starts with 5 and doesn't have +, add +90
    if phone.startswith('5') and not phone.startswith('+'):
        phone = '+90' + phone
    
    return phone

def clean_csv_file(input_file, output_file):
    """Clean phone numbers in a CSV file."""
    print(f"Processing {input_file}...")
    
    # Read CSV
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    
    # Find phone columns (those containing 'Phone' and 'Value')
    if not rows:
        raise ValueError("CSV dosyasi bos.")
    header = rows[0]
    phone_columns = [i for i, col in enumerate(header) if 'Phone' in col and 'Value' in col]
    
    print(f"Found phone columns at indices: {phone_columns}")
    
    # Clean each phone column
    for row in rows[1:]:  # Skip header
        for col_idx in phone_columns:
            if col_idx < len(row):
                row[col_idx] = clean_phone_number(row[col_idx])
    
    # Save to output file
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    
    print(f"Saved cleaned data to {output_file}")

if __name__ == "__main__":
    sources = (
        (BASE / "ma_contacts2026.csv", BASE / "ma_contacts2026_cleaned.csv"),
        (BASE / "ma_customer_list_2026.csv", BASE / "ma_customer_list_2026_cleaned.csv"),
    )
    for source, target in sources:
        if source.exists():
            clean_csv_file(source, target)
        else:
            print(f"Atlandi, dosya bulunamadi: {source}")
    print("\nDone!")
