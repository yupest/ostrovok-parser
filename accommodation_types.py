"""Provider property types; aparthotels remain in the hotels tab."""
def is_apartment(kind):
    return str(kind or '').strip().replace('_','').replace(' ','').lower() in {'apartment','apartments','flat','flats'}
